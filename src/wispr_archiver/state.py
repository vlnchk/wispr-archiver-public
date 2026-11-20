"""SQLite state. Derived data: `rebuild-state` restores it from the JSON archive.

Statuses: ok, pending (not finalized yet), error (attempt failed, will retry),
failed (max attempts exhausted in a run; still retried),
upstream_transcript_removed / upstream_transcript_shrunk (guard kept the old version).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    modified_at TEXT,
    content_sha256 TEXT,
    transcript_chars INTEGER,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    first_archived_at TEXT,
    last_seen_at TEXT,
    deleted_upstream_at TEXT,
    reappeared_at TEXT,
    archive_path TEXT,
    upstream_modified_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    completed INTEGER NOT NULL DEFAULT 0,
    alerted INTEGER NOT NULL DEFAULT 0,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS errors (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    meeting_id TEXT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT
);
"""

RETRY_STATUSES = ("pending", "error", "failed")
GUARD_STATUSES = ("upstream_transcript_removed", "upstream_transcript_shrunk")


def new_run_id() -> str:
    """The managed job passes its systemd invocation ID; manual runs get independent IDs."""
    invocation = os.environ.get("WISPR_ARCHIVER_RUN_ID", "")
    return invocation if re.fullmatch(r"[0-9a-f]{32}", invocation) else secrets.token_hex(6)


@dataclass(frozen=True)
class MeetingRow:
    id: str
    modified_at: str | None
    content_sha256: str | None
    transcript_chars: int | None
    status: str
    attempts: int
    last_error: str | None
    first_archived_at: str | None
    last_seen_at: str | None
    deleted_upstream_at: str | None
    reappeared_at: str | None
    archive_path: str | None
    upstream_modified_at: str | None


class State:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", SCHEMA_VERSION)

    @classmethod
    def open(cls, path: Path, *, dry_run: bool = False) -> State:
        """Open the state. In dry-run the file is copied into memory and never written."""
        if dry_run:
            mem = sqlite3.connect(":memory:", isolation_level=None)
            if path.exists():
                src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    src.backup(mem)
                finally:
                    src.close()
            return cls(mem)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return cls(conn)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # --- meta ---------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # --- meetings -----------------------------------------------------------------------

    def get(self, meeting_id: str) -> MeetingRow | None:
        row = self.conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return None if row is None else MeetingRow(**dict(row))

    def all(self) -> list[MeetingRow]:
        return [
            MeetingRow(**dict(r)) for r in self.conn.execute("SELECT * FROM meetings ORDER BY id")
        ]

    def with_status(self, *statuses: str) -> list[MeetingRow]:
        marks = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"SELECT * FROM meetings WHERE status IN ({marks}) ORDER BY id",  # noqa: S608
            statuses,
        )
        return [MeetingRow(**dict(r)) for r in rows]

    def upsert(self, meeting_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        updates = ", ".join(f"{k}=excluded.{k}" for k in fields)
        if "status" not in fields:
            # New rows need a status; existing rows keep theirs.
            cols += ", status"
            marks += ", ?"
            values = [*fields.values(), "pending"]
        else:
            values = list(fields.values())
        self.conn.execute(
            f"INSERT INTO meetings(id, {cols}) VALUES(?, {marks}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            [meeting_id, *values],
        )

    def mark_seen(self, meeting_id: str, *, at: str, upstream_modified_at: str) -> None:
        self.upsert(meeting_id, last_seen_at=at, upstream_modified_at=upstream_modified_at)

    def mark_pending(self, meeting_id: str, *, upstream_modified_at: str) -> None:
        row = self.get(meeting_id)
        if row is None or row.status in ("pending", "error", "failed"):
            self.upsert(meeting_id, status="pending", upstream_modified_at=upstream_modified_at)
        else:
            # Already archived once and went back to non-final: keep the archived status.
            self.upsert(meeting_id, upstream_modified_at=upstream_modified_at)

    def mark_archived(
        self,
        meeting_id: str,
        *,
        status: str,
        modified_at: str,
        content_sha256: str,
        transcript_chars: int,
        archive_path: str,
        at: str,
    ) -> None:
        row = self.get(meeting_id)
        self.upsert(
            meeting_id,
            status=status,
            modified_at=modified_at,
            content_sha256=content_sha256,
            transcript_chars=transcript_chars,
            archive_path=archive_path,
            attempts=0,
            last_error=None,
            first_archived_at=(row.first_archived_at if row and row.first_archived_at else at),
            upstream_modified_at=modified_at,
        )

    def record_failure(self, meeting_id: str, *, kind: str, failed: bool) -> int:
        row = self.get(meeting_id)
        attempts = (row.attempts if row else 0) + 1
        self.upsert(
            meeting_id,
            attempts=attempts,
            last_error=kind,
            status="failed" if failed else "error",
        )
        return attempts

    def set_deleted(self, meeting_id: str, at: str | None) -> None:
        self.upsert(meeting_id, deleted_upstream_at=at)

    def set_reappeared(self, meeting_id: str, at: str) -> None:
        self.upsert(meeting_id, deleted_upstream_at=None, reappeared_at=at)

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) FROM meetings GROUP BY status")
        return {str(r[0]): int(r[1]) for r in rows}

    # --- runs / errors ------------------------------------------------------------------

    def start_run(self, run_id: str, command: str, at: str) -> None:
        self.conn.execute(
            "INSERT INTO runs(id, command, started_at) VALUES(?, ?, ?)", (run_id, command, at)
        )

    def finish_run(
        self, run_id: str, *, at: str, exit_code: int, completed: bool, summary: dict[str, Any]
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, exit_code=?, completed=?, summary=? WHERE id=?",
            (at, exit_code, int(completed), json.dumps(summary, sort_keys=True), run_id),
        )

    def run_alerted(self, run_id: str, command: str) -> bool | None:
        row = self.conn.execute(
            "SELECT alerted FROM runs WHERE id=? AND command=?", (run_id, command)
        ).fetchone()
        return None if row is None else bool(row[0])

    def mark_run_alerted(self, run_id: str) -> None:
        self.conn.execute("UPDATE runs SET alerted=1 WHERE id=?", (run_id,))

    def last_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def add_error(
        self, *, run_id: str, meeting_id: str | None, at: str, kind: str, message: str
    ) -> None:
        self.conn.execute(
            "INSERT INTO errors(run_id, meeting_id, at, kind, message) VALUES(?, ?, ?, ?, ?)",
            (run_id, meeting_id, at, kind, message[:500]),
        )
