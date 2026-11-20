"""Archive layout and atomic, append-only writes.

Layout under the archive root:
  meetings/YYYY/MM/YYYYMMDDTHHMM_<slug>__<id8>.{json,md}   main version (path pinned)
  _snapshots/<id>/<YYYYMMDDTHHMMSS.ffffffZ>__<sha8>.json    every version, never overwritten

Nothing here ever deletes a file. Files are created 0640 and directories 0750 whatever
the umask (the archive is private; a stricter umask still applies).
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import re
import secrets
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from wispr_archiver.archive.model import content_hash, record_json
from wispr_archiver.archive.render import LEGACY_LABELS, render_markdown
from wispr_archiver.errors import StoreError
from wispr_archiver.wispr.models import parse_ts

Action = Literal["new", "updated", "unchanged", "guarded"]

FILE_MODE = 0o640
DIR_MODE = 0o750

_SLUG_RE = re.compile(r"[\W_]+", re.UNICODE)


def slugify(title: str | None) -> str:
    text = unicodedata.normalize("NFC", title or "untitled").lower()
    slug = _SLUG_RE.sub("-", text).strip("-")[:60].strip("-")
    return slug or "untitled"


def _stamp(record: dict[str, Any]) -> tuple[str, str, str]:
    ts = parse_ts(record.get("start") or record["modified_at"]).astimezone(UTC)
    return f"{ts:%Y}", f"{ts:%m}", f"{ts:%Y%m%dT%H%M}"


def base_name(record: dict[str, Any], *, full_id: bool = False) -> str:
    _, _, stamp = _stamp(record)
    ident = record["id"] if full_id else record["id"][:8]
    return f"{stamp}_{slugify(record.get('title'))}__{ident}"


def snapshot_name(record: dict[str, Any]) -> str:
    ts = parse_ts(record["modified_at"]).astimezone(UTC)
    return f"{ts:%Y%m%dT%H%M%S.%fZ}__{record['content_sha256'][:8]}.json"


@dataclass(frozen=True)
class Decision:
    write: bool
    reason: str


def should_overwrite(
    prev: dict[str, Any] | None, new: dict[str, Any], *, shrink_threshold: float
) -> Decision:
    """Transcript guard: never replace the main file with a poorer transcript."""
    if prev is None:
        return Decision(True, "new")
    if prev["content_sha256"] == new["content_sha256"]:
        return Decision(False, "unchanged")
    prev_chars = int(prev.get("transcript_chars") or 0)
    new_chars = int(new.get("transcript_chars") or 0)
    if prev_chars > 0 and new_chars == 0:
        return Decision(False, "upstream_transcript_removed")
    if new_chars < prev_chars * (1 - shrink_threshold):
        return Decision(False, "upstream_transcript_shrunk")
    return Decision(True, "changed")


@dataclass(frozen=True)
class SaveOutcome:
    action: Action
    reason: str
    archive_path: str
    snapshot_written: bool
    bytes_written: int
    record: dict[str, Any]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _tmp_path(final: Path) -> Path:
    return final.with_name(f".{final.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")


def ensure_dir(path: Path) -> None:
    """mkdir -p where every created level gets DIR_MODE (Path.mkdir applies it to the last only)."""
    missing: list[Path] = []
    p = path
    while not p.is_dir():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        with contextlib.suppress(FileExistsError):
            d.mkdir(mode=DIR_MODE)  # the umask can only tighten it


def _write_tmp(final: Path, data: bytes) -> Path:
    ensure_dir(final.parent)
    tmp = _tmp_path(final)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)  # umask only tightens
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    return tmp


def atomic_write(final: Path, data: bytes) -> int:
    """temp -> fsync -> rename -> fsync(dir). Replaces `final` if it exists."""
    tmp = _write_tmp(final, data)
    try:
        tmp.replace(final)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    _fsync_dir(final.parent)
    return len(data)


def write_new_only(final: Path, data: bytes) -> bool:
    """Create `final` atomically; never overwrite. Returns False if it already exists."""
    tmp = _write_tmp(final, data)
    try:
        os.link(tmp, final)
    except FileExistsError:
        return False
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    _fsync_dir(final.parent)
    return True


def _encode(text: str, what: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StoreError(f"{what}: text is not valid unicode") from exc


def read_record(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreError("unreadable archive file") from exc
    if not isinstance(data, dict) or "id" not in data:
        raise StoreError("archive file is not a record")
    return data


class ArchiveStore:
    def __init__(
        self, root: Path, tz: ZoneInfo, *, shrink_threshold: float, dry_run: bool = False
    ) -> None:
        self.root = root
        self.tz = tz
        self.shrink_threshold = shrink_threshold
        self.dry_run = dry_run
        self.meetings_dir = root / "meetings"
        self.snapshots_dir = root / "_snapshots"

    # --- locating -----------------------------------------------------------------------

    def _checked_path(self, path: Path, directory: Path) -> Path:
        # Keep writes in the selected archive subtree, including after symlink resolution.
        if not path.is_relative_to(directory) or not path.resolve().is_relative_to(
            self.root.resolve() / directory.name
        ):
            raise StoreError("path outside archive directory")
        return path

    def snapshot_directory(self, meeting_id: str) -> Path:
        if (
            not meeting_id
            or meeting_id in {".", ".."}
            or Path(meeting_id).name != meeting_id
            or "\\" in meeting_id
            or not meeting_id.isprintable()
        ):
            raise StoreError("invalid meeting ID for archive path")
        return self._checked_path(self.snapshots_dir / meeting_id, self.snapshots_dir)

    def _id_of(self, path: Path) -> str | None:
        try:
            return str(read_record(path)["id"])
        except StoreError:
            return None

    def locate(self, meeting_id: str, hint: str | None = None) -> Path | None:
        """Find the pinned main file of a meeting (state hint first, then by id suffix)."""
        self.snapshot_directory(meeting_id)
        if hint:
            p = self._checked_path(self.root / hint, self.meetings_dir)
            if p.is_file() and self._id_of(p) == meeting_id:
                return p
        for pattern in (
            f"*/*/*__{glob.escape(meeting_id[:8])}.json",
            f"*/*/*__{glob.escape(meeting_id)}.json",
        ):
            for p in sorted(self.meetings_dir.glob(pattern)):
                if self._id_of(self._checked_path(p, self.meetings_dir)) == meeting_id:
                    return p
        return None

    def _new_path(self, record: dict[str, Any]) -> Path:
        year, month, _ = _stamp(record)
        p = self.meetings_dir / year / month / f"{base_name(record)}.json"
        if p.exists() and self._id_of(p) != record["id"]:
            p = self.meetings_dir / year / month / f"{base_name(record, full_id=True)}.json"
        return p

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # --- writing ------------------------------------------------------------------------

    def _write_snapshot(self, record: dict[str, Any]) -> tuple[bool, int]:
        final = self._checked_path(
            self.snapshot_directory(record["id"]) / snapshot_name(record), self.snapshots_dir
        )
        if final.exists():
            existing = read_record(final)
            if existing.get("content_sha256") != record["content_sha256"]:
                raise StoreError(f"snapshot clash for {record['id']}")
            return False, 0
        data = _encode(record_json(record), "snapshot")
        if self.dry_run:
            return True, len(data)
        if not write_new_only(final, data):
            return False, 0
        return True, len(data)

    def _repair_markdown(self, path: Path, record: dict[str, Any]) -> int:
        data = _encode(render_markdown(record, self.tz), "markdown")
        md = self._checked_path(path.with_suffix(".md"), self.meetings_dir)
        if md.is_file() and _matches_markdown(md.read_bytes(), data, record, self.tz):
            return 0
        return len(data) if self.dry_run else atomic_write(md, data)

    def _write_main(self, path: Path, record: dict[str, Any]) -> int:
        self._checked_path(path, self.meetings_dir)
        self._checked_path(path.with_suffix(".md"), self.meetings_dir)
        js = _encode(record_json(record), "record")
        md = _encode(render_markdown(record, self.tz), "markdown")
        if self.dry_run:
            return len(js) + len(md)
        return atomic_write(path, js) + atomic_write(path.with_suffix(".md"), md)

    def save(self, record: dict[str, Any], *, hint: str | None = None) -> SaveOutcome:
        existing = self.locate(record["id"], hint)
        prev = read_record(existing) if existing is not None else None
        decision = should_overwrite(prev, record, shrink_threshold=self.shrink_threshold)
        path = existing if existing is not None else self._new_path(record)
        rel = self.rel(path)
        record = {**record, "archive_path": rel}
        if prev is not None:
            record["deleted_upstream_at"] = prev.get("deleted_upstream_at")
            record["reappeared_at"] = prev.get("reappeared_at")

        if decision.reason == "unchanged" and prev is not None:
            written = self._repair_markdown(path, prev)
            return SaveOutcome("unchanged", "unchanged", rel, False, written, prev)

        snap_written, written = self._write_snapshot(record)
        if not decision.write and prev is not None:
            written += self._repair_markdown(path, prev)
            return SaveOutcome("guarded", decision.reason, rel, snap_written, written, prev)
        written += self._write_main(path, record)
        action: Action = "new" if prev is None else "updated"
        return SaveOutcome(action, decision.reason, rel, snap_written, written, record)

    def set_flags(self, rel: str, **flags: str | None) -> int:
        """Update bookkeeping fields (deleted_upstream_at, reappeared_at). Never deletes."""
        path = self._checked_path(self.root / rel, self.meetings_dir)
        record = read_record(path)
        if all(record.get(k) == v for k, v in flags.items()):
            return self._repair_markdown(path, record)
        record.update(flags)
        return self._write_main(path, record)

    # --- reading ------------------------------------------------------------------------

    def iter_main(self) -> Iterator[Path]:
        yield from sorted(self.meetings_dir.glob("*/*/*.json"))

    def iter_snapshots(self) -> Iterator[Path]:
        yield from sorted(self.snapshots_dir.glob("*/*.json"))


@dataclass
class VerifyReport:
    checked: int = 0
    snapshots: int = 0
    problems: list[tuple[str, str]] | None = None

    def add(self, meeting_id: str, problem: str) -> None:
        if self.problems is None:
            self.problems = []
        self.problems.append((meeting_id, problem))

    @property
    def ok(self) -> bool:
        return not self.problems


def _matches_markdown(actual: bytes, expected: bytes, record: dict[str, Any], tz: ZoneInfo) -> bool:
    return actual == expected or actual == render_markdown(record, tz, LEGACY_LABELS).encode()


def verify_archive(store: ArchiveStore) -> VerifyReport:
    """Recompute hashes, check lengths, MD/JSON consistency and snapshot coverage."""
    report = VerifyReport()
    if not store.meetings_dir.is_dir():
        report.add("", "archive_missing")
        return report
    snap_shas: dict[str, set[str]] = {}
    for path in store.iter_snapshots():
        report.snapshots += 1
        mid = path.parent.name
        try:
            rec = read_record(path)
        except StoreError:
            report.add(mid, "snapshot_unreadable")
            continue
        sha = content_hash(rec)
        if sha != rec.get("content_sha256"):
            report.add(mid, "snapshot_hash_mismatch")
        if not path.name.endswith(f"__{sha[:8]}.json"):
            report.add(mid, "snapshot_name_mismatch")
        snap_shas.setdefault(mid, set()).add(sha)

    for path in store.iter_main():
        report.checked += 1
        try:
            rec = read_record(path)
        except StoreError:
            report.add(path.stem[-8:], "record_unreadable")
            continue
        mid = str(rec["id"])
        sha = content_hash(rec)
        if sha != rec.get("content_sha256"):
            report.add(mid, "hash_mismatch")
        if rec.get("transcript_chars") != len(rec.get("transcript_text") or ""):
            report.add(mid, "transcript_length_mismatch")
        if rec.get("archive_path") != store.rel(path):
            report.add(mid, "archive_path_mismatch")
        md = path.with_suffix(".md")
        if not md.is_file():
            report.add(mid, "markdown_missing")
        else:
            try:
                expected = render_markdown(rec, store.tz).encode()
                matches = _matches_markdown(md.read_bytes(), expected, rec, store.tz)
            except (OSError, KeyError, ValueError, TypeError, AttributeError):
                matches = False
            if not matches:
                report.add(mid, "markdown_out_of_date")
        if sha not in snap_shas.get(mid, set()):
            report.add(mid, "snapshot_missing")
    return report
