"""Replicate the local archive to Google Drive with rclone.

Only two rclone subcommands are ever built: `copy` (never deletes on the destination)
and `check` (read-only). `_snapshots/` is copied with `--immutable`, so a snapshot that
already exists on the remote can never be changed, only reported. The main copy carries
only `meetings/`. rclone's log lines name files, and file names carry meeting titles, so
they are parsed for counters and never logged or put into alerts.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wispr_archiver.config import Settings
from wispr_archiver.errors import ExitCode, PublishError
from wispr_archiver.lock import run_lock
from wispr_archiver.log import event, warn
from wispr_archiver.notify import RUNBOOK, Alert, AlertDispatcher, Notifier, make_notifier
from wispr_archiver.state import State, new_run_id
from wispr_archiver.wispr.client import iso_z

ALLOWED_SUBCOMMANDS = frozenset({"copy", "check"})
#: Must never appear as a token of an rclone command line (enforced here and in tests).
FORBIDDEN_TOKENS = frozenset(
    {
        "sync",
        "bisync",
        "move",
        "moveto",
        "delete",
        "deletefile",
        "purge",
        "rmdir",
        "rmdirs",
        "dedupe",
        "cleanup",
    }
)

EXCLUDE_TMP = "- .*.tmp-*"  # temp files of atomic writes (archive/store.py)
MAIN_FILTERS = (EXCLUDE_TMP, "+ /meetings/**", "- **")
SNAPSHOT_FILTERS = (EXCLUDE_TMP,)
CHECK_FILTERS = (EXCLUDE_TMP, "+ /meetings/**", "+ /_snapshots/**", "- **")

#: rclone / Google error texts with a known operator action (RUNBOOK, «Google Drive»).
#: Matched case-insensitively in error lines; the text itself is never logged.
KNOWN_FAILURES = (
    ("didn't find section in config file", "remote_not_configured"),
    ("invalid_grant", "drive_auth"),
    ("ratelimitexceeded", "drive_quota"),
    ("quotaexceeded", "drive_quota"),
)

#: Counters taken from rclone's final JSON stats line.
STAT_KEYS = ("transfers", "bytes", "checks", "errors", "deletes", "deletedDirs", "renames")
#: Any of these above zero means something was removed or moved on the remote.
DESTRUCTIVE_STATS = ("deletes", "deletedDirs", "renames")

ENV_PASSTHROUGH = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)

PUBLISH_TEXT = (
    "Google Drive publication failed ({problems}). The next run will retry. See "
    + RUNBOOK
    + "#google-drive."
)
VERIFY_REMOTE_TEXT = (
    "Google Drive verification failed ({problems}). See " + RUNBOOK + "#google-drive."
)


@dataclass(frozen=True)
class ProcResult:
    returncode: int
    stderr: str


Runner = Callable[[list[str], dict[str, str], float], ProcResult]


def subprocess_runner(argv: list[str], env: dict[str, str], timeout: float) -> ProcResult:
    try:
        proc = subprocess.run(  # noqa: S603 - argv is built by Rclone.argv from a whitelist
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PublishError("rclone binary not found", kind="rclone_missing") from exc
    except subprocess.TimeoutExpired as exc:
        raise PublishError("rclone timed out", kind="rclone_timeout") from exc
    return ProcResult(proc.returncode, proc.stderr)


@dataclass(frozen=True)
class RcloneOutcome:
    exit_code: int
    stats: dict[str, int]
    error_lines: int
    duration_ms: int
    reasons: tuple[str, ...] = ()


def _reasons_in(text: str) -> list[str]:
    low = text.lower()
    return [reason for needle, reason in KNOWN_FAILURES if needle in low]


def parse_rclone_log(stderr: str) -> tuple[dict[str, int], int, tuple[str, ...]]:
    """Counters from the last JSON stats line, the number of error lines and known failure
    reasons (KNOWN_FAILURES). The text itself is dropped."""
    stats: dict[str, int] = {}
    errors = 0
    reasons: list[str] = []
    for line in stderr.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            reasons += _reasons_in(line)  # early errors may come before JSON logging starts
            continue
        if not isinstance(data, dict):
            continue
        if data.get("level") in ("error", "critical"):
            errors += 1
            reasons += _reasons_in(str(data.get("msg", "")))
        raw = data.get("stats")
        if isinstance(raw, dict):
            stats = {
                k: int(raw[k])
                for k in STAT_KEYS
                if isinstance(raw.get(k), int | float) and not isinstance(raw.get(k), bool)
            }
    return stats, errors, tuple(dict.fromkeys(reasons))


def join_remote(remote: str, sub: str) -> str:
    return f"{remote.rstrip('/')}/{sub}"


class Rclone:
    def __init__(
        self,
        s: Settings,
        *,
        runner: Runner = subprocess_runner,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.binary = s.rclone_binary
        self.config = s.rclone_config
        self.timeout = s.publish_timeout_seconds
        self.runner = runner
        source = os.environ if environ is None else environ
        # Only what rclone needs: never pass the Telegram token or other secrets along.
        self.env = {
            k: v for k, v in source.items() if k in ENV_PASSTHROUGH or k.startswith("RCLONE_")
        }

    def argv(self, sub: str, *args: str) -> list[str]:
        if sub not in ALLOWED_SUBCOMMANDS:
            raise ValueError(f"rclone subcommand not allowed: {sub}")
        argv = [self.binary, sub, *args]
        argv += ["--use-json-log", "--log-level", "NOTICE"]
        argv += ["--stats", "1h", "--stats-log-level", "NOTICE"]  # one final stats line
        if self.config is not None:
            argv += ["--config", str(self.config)]
        bad = FORBIDDEN_TOKENS.intersection(argv)
        if bad:
            raise ValueError(f"forbidden rclone token: {sorted(bad)}")
        return argv

    def run(self, sub: str, *args: str) -> RcloneOutcome:
        argv = self.argv(sub, *args)
        started = time.monotonic()
        proc = self.runner(argv, dict(self.env), self.timeout)
        stats, errors, reasons = parse_rclone_log(proc.stderr)
        return RcloneOutcome(
            exit_code=proc.returncode,
            stats=stats,
            error_lines=errors,
            duration_ms=int((time.monotonic() - started) * 1000),
            reasons=reasons,
        )


def _filters(rules: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for rule in rules:
        out += ["--filter", rule]
    return out


def _local_files(root: Path) -> Iterator[Path]:
    for top in ("meetings", "_snapshots"):
        for p in (root / top).rglob("*"):
            if p.is_file() and not (p.name.startswith(".") and ".tmp-" in p.name):
                yield p


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Run:
    """Run bookkeeping shared by publish and verify-remote: runs table, alert, final line."""

    command: str
    s: Settings
    state: State
    alerts: AlertDispatcher
    now: Callable[[], datetime]
    dry_run: bool = False
    run_id: str = field(default_factory=new_run_id)
    problems: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    def start(self) -> None:
        self.state.start_run(self.run_id, self.command, iso_z(self.now()))
        event("run_started", run_id=self.run_id, command=self.command, dry_run=self.dry_run)

    def finish(self, summary: dict[str, Any], alert_text: str) -> ExitCode:
        code = ExitCode.PARTIAL if self.problems else ExitCode.OK
        if self.problems:
            self.alerts.dispatch(
                Alert(
                    kind=self.command,
                    text=alert_text.format(problems=", ".join(self.problems)),
                    key=",".join(sorted(self.problems)),
                ),
                now=self.now(),
                run_id=self.run_id,
            )
        self.state.finish_run(
            self.run_id,
            at=iso_z(self.now()),
            exit_code=int(code),
            completed=True,
            summary={**summary, "problems": self.problems},
        )
        event(
            "run_finished",
            run_id=self.run_id,
            command=self.command,
            exit_code=int(code),
            duration_ms=int((time.monotonic() - self.started) * 1000),
            dry_run=self.dry_run,
            problems=self.problems,
            **summary,
        )
        return code


def _step(run: _Run, rclone: Rclone, step: str, *args: str) -> RcloneOutcome | None:
    try:
        out = rclone.run("copy", *args)
    except PublishError as exc:
        run.problems.append(f"{step}:{exc.kind}")
        warn("publish_step", step=step, error_kind=exc.kind)
        return None
    destructive = [k for k in DESTRUCTIVE_STATS if out.stats.get(k, 0) > 0]
    if out.exit_code != 0:
        named = [f"{step}:{r}" for r in out.reasons]
        run.problems += named or [f"{step}:rclone_exit_{out.exit_code}"]
    if destructive:
        run.problems.append(f"{step}:unexpected_deletes")
    event(
        "publish_step",
        step=step,
        rclone_exit=out.exit_code,
        reason=",".join(out.reasons) or None,
        duration_ms=out.duration_ms,
        **{k: out.stats.get(k, 0) for k in ("transfers", "bytes", "checks", "errors", "deletes")},
    )
    return out


def _open(
    s: Settings, *, dry_run: bool, notifier: Notifier | None
) -> tuple[State, AlertDispatcher]:
    state = State.open(s.state_path, dry_run=dry_run)
    alerts = AlertDispatcher(
        state, notifier or make_notifier(s, dry_run=dry_run), dedup_hours=s.alert_dedup_hours
    )
    return state, alerts


def run_publish(
    s: Settings,
    *,
    dry_run: bool = False,
    rclone: Rclone | None = None,
    notifier: Notifier | None = None,
    now: Callable[[], datetime] = _now,
) -> ExitCode:
    """`rclone copy` of _snapshots/ (immutable) and meetings/ to `rclone_remote`."""
    rclone = rclone or Rclone(s)
    with run_lock(s.lock_path):
        state, alerts = _open(s, dry_run=dry_run, notifier=notifier)
        try:
            run = _Run("publish", s, state, alerts, now, dry_run=dry_run)
            run.start()
            root = s.archive_dir.resolve()
            dry = ["--dry-run"] if dry_run else []
            outcomes: list[RcloneOutcome] = []
            if not (root / "meetings").is_dir():
                run.problems.append("archive_missing")
            else:
                if (root / "_snapshots").is_dir():
                    out = _step(
                        run,
                        rclone,
                        "snapshots",
                        str(root / "_snapshots"),
                        join_remote(s.rclone_remote, "_snapshots"),
                        "--immutable",
                        "--checksum",
                        *_filters(SNAPSHOT_FILTERS),
                        *dry,
                    )
                    outcomes += [out] if out else []
                out = _step(
                    run,
                    rclone,
                    "main",
                    str(root),
                    s.rclone_remote,
                    "--checksum",
                    *_filters(MAIN_FILTERS),
                    *dry,
                )
                outcomes += [out] if out else []
            summary = {
                k: sum(o.stats.get(k, 0) for o in outcomes)
                for k in ("transfers", "bytes", "errors")
            }
            if not run.problems and not dry_run:
                state.set_meta("last_published_at", iso_z(now()))
            return run.finish(summary, PUBLISH_TEXT)
        finally:
            state.close()


def _count_lines(path: Path) -> int:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if line.strip())
    except FileNotFoundError:
        return 0


def run_verify_remote(
    s: Settings,
    *,
    rclone: Rclone | None = None,
    notifier: Notifier | None = None,
    report: dict[str, Any] | None = None,
    now: Callable[[], datetime] = _now,
) -> ExitCode:
    """`rclone check --one-way`: every local archive file exists on the remote, same hash."""
    rclone = rclone or Rclone(s)
    report = {} if report is None else report
    with run_lock(s.lock_path):
        state, alerts = _open(s, dry_run=False, notifier=notifier)
        try:
            run = _Run("verify-remote", s, state, alerts, now)
            run.start()
            root = s.archive_dir.resolve()
            if not (root / "meetings").is_dir():
                run.problems.append("archive_missing")
                report.update(ok=False)
                return run.finish({}, VERIFY_REMOTE_TEXT)
            local_files = sum(1 for _ in _local_files(root))
            with tempfile.TemporaryDirectory(prefix="wispr-archiver-check-") as tmp:
                files = {
                    k: Path(tmp) / f"{k}.txt" for k in ("missing", "differ", "errors", "matched")
                }
                rclone_exit = -1
                try:
                    out = rclone.run(
                        "check",
                        str(root),
                        s.rclone_remote,
                        "--one-way",
                        *_filters(CHECK_FILTERS),
                        "--missing-on-dst",
                        str(files["missing"]),
                        "--differ",
                        str(files["differ"]),
                        "--error",
                        str(files["errors"]),
                        "--match",
                        str(files["matched"]),
                    )
                    rclone_exit = out.exit_code
                    if rclone_exit != 0:
                        run.problems += list(out.reasons)
                except PublishError as exc:
                    run.problems.append(exc.kind)
                counts = {k: _count_lines(p) for k, p in files.items()}
            for key in ("missing", "differ", "errors"):
                if counts[key]:
                    run.problems.append(key)
            if rclone_exit not in (0, -1) and not run.problems:
                run.problems.append(f"rclone_exit_{rclone_exit}")
            if not run.problems and counts["matched"] != local_files:
                run.problems.append("unmatched")
            report.update(
                local_files=local_files,
                matched=counts["matched"],
                missing=counts["missing"],
                differ=counts["differ"],
                errors=counts["errors"],
                rclone_exit=rclone_exit,
                ok=not run.problems,
            )
            if not run.problems:
                state.set_meta("last_verified_remote_at", iso_z(now()))
            summary = {k: v for k, v in report.items() if k != "ok"}
            return run.finish(summary, VERIFY_REMOTE_TEXT)
        finally:
            state.close()
