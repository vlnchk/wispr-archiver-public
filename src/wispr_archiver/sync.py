"""backfill / sync / reconcile engine.

Rules:
- candidates: finalized AND (new id OR modified_at changed OR status pending/error/failed);
- incremental listing stops at modified_at < watermark - overlap (list is modified_at DESC);
- the watermark moves only after a run without errors, and never past a pending meeting;
- up to `max_attempts` attempts per meeting per run with exponential backoff + jitter;
- FormatError (exit 3) and ReauthRequired (exit 2) stop the run at once;
- nothing is ever deleted: missing meetings get `deleted_upstream_at` (reconcile only).
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from wispr_archiver.archive.model import build_record
from wispr_archiver.archive.store import ArchiveStore
from wispr_archiver.config import Settings
from wispr_archiver.errors import (
    ArchiverError,
    CircuitOpen,
    ExitCode,
    FormatError,
    ReauthRequired,
    RunTimeout,
    StoreError,
    ToolError,
    worst,
)
from wispr_archiver.log import error as log_error
from wispr_archiver.log import event, warn
from wispr_archiver.notify import FORMAT_TEXT, REAUTH_TEXT, Alert, AlertDispatcher
from wispr_archiver.state import RETRY_STATUSES, MeetingRow, State, new_run_id
from wispr_archiver.wispr.client import Clock, WisprApi, iso_z
from wispr_archiver.wispr.fetch import FetchedMeeting, fetch_meeting
from wispr_archiver.wispr.models import ListedMeeting, SearchPage, parse_ts

Command = Literal["backfill", "sync", "reconcile"]

#: Lower bound for window splitting when a listing is truncated (>1000 results).
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
MIN_WINDOW = timedelta(minutes=1)
BACKOFF_BASE = 2.0
BACKOFF_CAP = 60.0
FORMAT_STREAK_ABORT = 3


@dataclass
class RunSummary:
    command: str
    dry_run: bool = False
    listed: int = 0
    candidates: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    guarded: int = 0
    failed: int = 0
    pending: int = 0
    snapshots: int = 0
    deleted_marked: int = 0
    reappeared: int = 0
    bytes_written: int = 0
    retries: int = 0  # failed attempts that were retried
    blocked: int = 0  # reconcile refused to mark deletions
    format_errors: int = 0  # meetings whose response did not match the contract
    not_found: int = 0
    calls: int = 0

    def fields(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Listing:
    meetings: list[ListedMeeting] = field(default_factory=list)
    complete: bool = False
    truncated: bool = False


class Archiver:
    def __init__(
        self,
        *,
        settings: Settings,
        api: WisprApi,
        store: ArchiveStore,
        state: State,
        clock: Clock,
        alerts: AlertDispatcher,
        dry_run: bool = False,
        rng: random.Random | None = None,
        run_id: str | None = None,
        preflight: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self.s = settings
        self.api = api
        self.store = store
        self.state = state
        self.clock = clock
        self.alerts = alerts
        self.dry_run = dry_run
        self.rng = rng or random.Random()  # noqa: S311 - jitter, not crypto
        self.run_id = run_id or new_run_id()
        self.preflight = preflight
        self.failed_ids: list[str] = []
        self.format_ids: list[str] = []
        self.format_details: list[str] = []
        self._format_streak = 0
        self.summary = RunSummary(command="", dry_run=dry_run)

    # --- helpers ------------------------------------------------------------------------

    def now(self) -> str:
        return iso_z(self.clock.utcnow())

    def _backoff(self, attempt: int) -> float:
        base = min(BACKOFF_CAP, BACKOFF_BASE * 2.0 ** (attempt - 1))
        return base * (0.5 + self.rng.random())

    async def _retrying[T](self, what: str, op: Callable[[], Awaitable[T]]) -> T:
        """Retry a non-meeting call (listing pages) with backoff; same budget as meetings."""
        for attempt in range(1, self.s.max_attempts + 1):
            try:
                return await op()
            except ToolError as exc:
                if not exc.retryable or attempt == self.s.max_attempts:
                    raise
                self.summary.retries += 1
                warn("call_retry", tool=what, attempt=attempt, error_kind=exc.kind)
                await self.api.sleep(self._backoff(attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    # --- listing ------------------------------------------------------------------------

    async def _list_pages(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        stop_before: datetime | None = None,
    ) -> Listing:
        out = Listing()
        cursor: str | None = None
        prev_mod: datetime | None = None
        page_no = 0
        while True:
            page_no += 1

            async def get_page(cursor: str | None = cursor) -> SearchPage:
                return await self.api.search_meetings(
                    limit=self.s.search_limit,
                    cursor=cursor,
                    since=since.isoformat() if since else None,
                    until=until.isoformat() if until else None,
                )

            page = await self._retrying("search_meetings", get_page)
            event(
                "list_page",
                page=page_no,
                count=len(page.meetings),
                has_more=page.has_more,
                truncated=page.truncated,
                cursor_present=page.next_cursor is not None,
            )
            if page.truncated:
                out.truncated = True
                return out
            for m in page.meetings:
                mod = parse_ts(m.modified_at)
                if prev_mod is not None and mod > prev_mod:
                    raise FormatError("search_meetings: results not sorted by modified_at desc")
                prev_mod = mod
                if stop_before is not None and mod < stop_before:
                    out.complete = True
                    return out
                out.meetings.append(m)
            if not page.has_more:
                out.complete = True
                return out
            if not page.next_cursor:
                raise FormatError("search_meetings: has_more without next_cursor")
            cursor = page.next_cursor

    async def _list_window(
        self, since: datetime | None, until: datetime | None
    ) -> list[ListedMeeting]:
        res = await self._list_pages(since=since, until=until)
        if not res.truncated:
            return res.meetings
        lo = since or EPOCH
        hi = until or (self.clock.utcnow() + timedelta(days=1))
        if hi - lo < MIN_WINDOW:
            raise ArchiverError("listing still truncated in a one-minute window")
        mid = lo + (hi - lo) / 2
        event("list_split", window_since=lo.isoformat(), window_until=hi.isoformat())
        merged: dict[str, ListedMeeting] = {}
        for part in (await self._list_window(lo, mid), await self._list_window(mid, hi)):
            for m in part:
                merged.setdefault(m.id, m)
        return sorted(merged.values(), key=lambda m: parse_ts(m.modified_at), reverse=True)

    async def list_all(self) -> list[ListedMeeting]:
        return await self._list_window(None, None)

    # --- per-meeting processing ---------------------------------------------------------

    def _is_candidate(self, m: ListedMeeting, row: MeetingRow | None) -> bool:
        return row is None or row.modified_at != m.modified_at or row.status in RETRY_STATUSES

    def _handle_seen(self, listed: list[ListedMeeting]) -> None:
        now = self.now()
        for m in listed:
            row = self.state.get(m.id)
            self.state.mark_seen(m.id, at=now, upstream_modified_at=m.modified_at)
            if row is not None and row.deleted_upstream_at:
                if row.archive_path:
                    self.summary.bytes_written += self.store.set_flags(
                        row.archive_path, deleted_upstream_at=None, reappeared_at=now
                    )
                self.state.set_reappeared(m.id, now)
                self.summary.reappeared += 1
                event("meeting_reappeared", meeting_id=m.id)

    async def _process(self, meeting_id: str, *, shared_hint: bool) -> None:
        for attempt in range(1, self.s.max_attempts + 1):
            try:
                await self._archive_one(meeting_id, shared_hint=shared_hint)
                return
            except ToolError as exc:
                if exc.kind == "not_found":
                    self.summary.not_found += 1
                    warn("meeting_not_found", meeting_id=meeting_id)
                    return
                if exc.kind == "account_disabled":
                    raise
                kind = exc.kind
                message = str(exc)
            except StoreError as exc:
                kind, message = exc.kind, str(exc)
            except FormatError as exc:
                self._meeting_format_error(meeting_id, exc)
                return
            failed = attempt == self.s.max_attempts
            attempts = self.state.record_failure(meeting_id, kind=kind, failed=failed)
            self.state.add_error(
                run_id=self.run_id, meeting_id=meeting_id, at=self.now(), kind=kind, message=message
            )
            warn(
                "meeting_attempt_failed",
                meeting_id=meeting_id,
                attempt=attempt,
                attempts=attempts,
                error_kind=kind,
                status="failed" if failed else "error",
            )
            if failed:
                self.summary.failed += 1
                self.failed_ids.append(meeting_id)
                return
            self.summary.retries += 1
            await self.api.sleep(self._backoff(attempt))

    def _meeting_format_error(self, meeting_id: str, exc: FormatError) -> None:
        """One meeting's response does not match the contract: nothing of it is written, it is
        marked failed (retried every run) and the run continues, so a single odd meeting
        cannot stop the archiving of all others. Three in a row look like a global format
        change and abort the run."""
        self.state.record_failure(meeting_id, kind="format", failed=True)
        self.state.add_error(
            run_id=self.run_id,
            meeting_id=meeting_id,
            at=self.now(),
            kind="format",
            message=str(exc),
        )
        self.summary.format_errors += 1
        self.format_ids.append(meeting_id)
        self.format_details.append(str(exc))
        self._format_streak += 1
        log_error("meeting_format_error", meeting_id=meeting_id, error=str(exc))
        if self._format_streak >= FORMAT_STREAK_ABORT:
            raise FormatError(f"{self._format_streak} meetings in a row: {exc}")

    async def _archive_one(self, meeting_id: str, *, shared_hint: bool) -> None:
        row = self.state.get(meeting_id)
        fetched = await fetch_meeting(
            self.api, meeting_id, chunk=self.s.chunk_size, shared_hint=shared_hint
        )
        if not isinstance(fetched, FetchedMeeting):
            self.state.mark_pending(meeting_id, upstream_modified_at=fetched.modified_at)
            self.summary.pending += 1
            event("meeting_pending", meeting_id=meeting_id, modified_at=fetched.modified_at)
            return
        now = self.now()
        record = build_record(
            meeting=fetched.meeting,
            participants=fetched.participants,
            transcript=fetched.transcript,
            notes=fetched.notes,
            archived_at=now,
        )
        outcome = self.store.save(record, hint=row.archive_path if row else None)
        status = "ok" if outcome.action != "guarded" else outcome.reason
        self.state.mark_archived(
            meeting_id,
            status=status,
            modified_at=fetched.meeting.modified_at,
            content_sha256=outcome.record["content_sha256"],
            transcript_chars=int(outcome.record["transcript_chars"]),
            archive_path=outcome.archive_path,
            at=now,
        )
        self._format_streak = 0
        s = self.summary
        setattr(s, outcome.action, getattr(s, outcome.action) + 1)
        s.snapshots += int(outcome.snapshot_written)
        s.bytes_written += outcome.bytes_written
        event(
            "meeting_" + outcome.action,
            meeting_id=meeting_id,
            reason=outcome.reason,
            modified_at=fetched.meeting.modified_at,
            content_sha256=record["content_sha256"],
            prev_sha256=row.content_sha256 if row else None,
            transcript_chars=record["transcript_chars"],
            notes_chars=len(fetched.notes),
            summary_chars=len(fetched.meeting.summary or ""),
            participants=len(fetched.participants.participants) if fetched.participants else 0,
            char_unit=fetched.transcript_unit,
            bytes=outcome.bytes_written,
        )

    async def _process_listing(self, listed: list[ListedMeeting]) -> None:
        self._handle_seen(listed)
        seen = set()
        for m in listed:
            seen.add(m.id)
            row = self.state.get(m.id)
            if not m.finalized:
                self.state.mark_pending(m.id, upstream_modified_at=m.modified_at)
                self.summary.pending += 1
                continue
            if not self._is_candidate(m, row):
                self.summary.skipped += 1
                continue
            self.summary.candidates += 1
            await self._process(m.id, shared_hint=bool(m.shared))
        # Retry rows the listing did not reach (pending / error / failed), unless gone upstream.
        for row in self.state.with_status(*RETRY_STATUSES):
            if row.id in seen or row.deleted_upstream_at:
                continue
            self.summary.candidates += 1
            await self._process(row.id, shared_hint=False)

    # --- watermark ----------------------------------------------------------------------

    def _advance_watermark(self, listed: list[ListedMeeting]) -> None:
        if self.summary.failed or self.summary.blocked or self.summary.format_errors:
            event("watermark_kept", watermark=self.state.get_meta("watermark"), reason="errors")
            return
        old = self.state.get_meta("watermark")
        candidates = [parse_ts(m.modified_at) for m in listed]
        if old:
            candidates.append(parse_ts(old))
        if not candidates:
            return
        new = max(candidates)
        pending = [
            parse_ts(r.upstream_modified_at)
            for r in self.state.with_status("pending")
            if r.upstream_modified_at and not r.deleted_upstream_at
        ]
        if pending:
            new = min(new, min(pending))
        value = iso_z(new)
        if value == old:
            return
        self.state.set_meta("watermark", value)
        event("watermark_moved", watermark=old, new_watermark=value)

    # --- reconcile ----------------------------------------------------------------------

    def _mark_deleted(self, listed: list[ListedMeeting]) -> None:
        ids = {m.id for m in listed}
        live = [r for r in self.state.all() if not r.deleted_upstream_at]
        archived = [r for r in live if r.archive_path]
        missing = [r for r in live if r.id not in ids]
        missing_archived = [r for r in missing if r.archive_path]
        fraction = len(missing_archived) / len(archived) if archived else 0.0
        blocked = (not listed and bool(archived)) or (
            len(missing_archived) >= self.s.reconcile_block_min_count
            and fraction > self.s.reconcile_max_deleted_fraction
        )
        if blocked:
            self.summary.blocked += 1
            warn("reconcile_blocked", count=len(missing_archived), fraction=round(fraction, 3))
            self.alerts.dispatch(
                Alert(
                    kind="reconcile_blocked",
                    text=(
                        "Reconcile skipped deletion markers. "
                        f"Wispr omitted {len(missing_archived)} of {len(archived)} "
                        f"archived meetings ({fraction:.0%}). "
                        "Check the account and the meeting list."
                    ),
                    key=str(len(missing_archived)),
                ),
                now=self.clock.utcnow(),
                run_id=self.run_id,
            )
            return
        now = self.now()
        for r in missing:
            if r.archive_path:
                self.summary.bytes_written += self.store.set_flags(
                    r.archive_path, deleted_upstream_at=now
                )
            self.state.set_deleted(r.id, now)
            self.summary.deleted_marked += 1
            event("meeting_deleted_upstream", meeting_id=r.id, status=r.status)

    # --- commands -----------------------------------------------------------------------

    async def _body(self, command: Command) -> bool:
        """Run the command. Returns True if the run completed (listing done, all processed)."""
        if self.preflight is not None:
            await self.preflight()  # e.g. proactive token refresh
        if command == "sync":
            wm = self.state.get_meta("watermark")
            stop = parse_ts(wm) - timedelta(hours=self.s.overlap_hours) if wm else None
            res = await self._list_pages(stop_before=stop)
            listed = res.meetings if not res.truncated else await self.list_all()
        else:
            listed = await self.list_all()
        self.summary.listed = len(listed)
        await self._process_listing(listed)
        if command == "reconcile":
            self._mark_deleted(listed)
        self._advance_watermark(listed)
        return True

    async def run(self, command: Command) -> ExitCode:
        self.summary = RunSummary(command=command, dry_run=self.dry_run)
        started = self.clock.monotonic()
        self.state.start_run(self.run_id, command, self.now())
        event("run_started", run_id=self.run_id, command=command, dry_run=self.dry_run)
        code = ExitCode.OK
        completed = False
        alert: Alert | None = None
        try:
            completed = await self._body(command)
        except ReauthRequired as exc:
            code = ExitCode.REAUTH
            alert = Alert(kind="reauth", text=REAUTH_TEXT)
            log_error(
                "run_aborted", error_class=type(exc).__name__, error_kind=exc.kind, error=str(exc)
            )
        except FormatError as exc:
            code = ExitCode.FORMAT
            alert = Alert(kind="format", text=FORMAT_TEXT.format(detail=str(exc)), key=str(exc))
            log_error(
                "run_aborted", error_class=type(exc).__name__, error_kind=exc.kind, error=str(exc)
            )
        except (RunTimeout, CircuitOpen, ToolError, StoreError, ArchiverError) as exc:
            code = ExitCode.PARTIAL
            alert = Alert(
                kind=f"aborted_{exc.kind}",
                text=f"The {command} run stopped ({exc.kind}). "
                f"The next run will retry from the same watermark. Details: {exc}",
                key=exc.kind,
            )
            log_error(
                "run_aborted", error_class=type(exc).__name__, error_kind=exc.kind, error=str(exc)
            )
        except OSError as exc:
            code = ExitCode.PARTIAL
            alert = Alert(
                kind="aborted_os_error",
                text=f"Disk or operating system error ({type(exc).__name__}).",
                key=type(exc).__name__,
            )
            log_error("run_aborted", error_class=type(exc).__name__)

        if completed and (self.summary.failed or self.summary.blocked):
            code = worst(code, ExitCode.PARTIAL)
        if self.format_ids:
            code = worst(code, ExitCode.FORMAT)
            if alert is None or alert.kind != "format":
                ids = sorted(set(self.format_ids))
                self.alerts.dispatch(
                    Alert(
                        kind="format",
                        text=FORMAT_TEXT.format(
                            detail=f"Meetings {', '.join(ids)}. {self.format_details[0]}"
                        ),
                        key=",".join(ids),
                    ),
                    now=self.clock.utcnow(),
                    run_id=self.run_id,
                )
        if alert is not None:
            self.alerts.dispatch(alert, now=self.clock.utcnow(), run_id=self.run_id)
        failed_rows = [r for r in self.state.with_status("failed") if r.last_error != "format"]
        if failed_rows:
            ids = sorted(r.id for r in failed_rows)
            self.alerts.dispatch(
                Alert(
                    kind="failed_meetings",
                    text=(
                        f"{len(ids)} meeting(s) could not be archived after {self.s.max_attempts} "
                        f"attempts: {', '.join(ids)}. Each run will retry. "
                        "Run `wispr-archiver status` for details."
                    ),
                    key=",".join(ids),
                ),
                now=self.clock.utcnow(),
                run_id=self.run_id,
            )

        self.summary.calls = self.api.calls
        now = self.now()
        if completed:
            self.state.set_meta("last_completed_at", now)
            if code == ExitCode.OK:
                self.state.set_meta("last_ok_at", now)
        self.state.finish_run(
            self.run_id,
            at=now,
            exit_code=int(code),
            completed=completed,
            summary=self.summary.fields(),
        )
        event(
            "run_finished",
            run_id=self.run_id,
            exit_code=int(code),
            duration_ms=int((self.clock.monotonic() - started) * 1000),
            **{k: v for k, v in self.summary.fields().items() if k != "command"},
        )
        return code
