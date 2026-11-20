"""Offline maintenance: rebuild-state and status (no network)."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from wispr_archiver.archive.store import ArchiveStore, read_record
from wispr_archiver.errors import StoreError
from wispr_archiver.state import State
from wispr_archiver.wispr.models import parse_ts


def _earliest_archived_at(store: ArchiveStore, meeting_id: str) -> str | None:
    stamps = []
    for snap in sorted(store.snapshot_directory(meeting_id).glob("*.json")):
        try:
            at = read_record(snap).get("archived_at")
        except StoreError:
            continue
        if at:
            stamps.append(str(at))
    return min(stamps) if stamps else None


def rebuild_state(store: ArchiveStore, state_path: Path, *, now: str) -> tuple[int, Path | None]:
    """Recreate the meetings table from the JSON archive.

    The old database (if any) is kept as `<name>.bak-<timestamp>`; nothing is deleted.
    The watermark is not restored, so the next sync lists everything once.
    """
    tmp = state_path.with_name(state_path.name + ".rebuild")
    for suffix in ("", "-wal", "-shm"):
        side = Path(str(tmp) + suffix)
        if side.exists():
            side.unlink()  # our own leftover temp file, not archive data
    st = State.open(tmp)
    count = 0
    try:
        with st.tx():
            for path in store.iter_main():
                rec = read_record(path)
                mid = str(rec["id"])
                st.upsert(
                    mid,
                    status="ok",
                    modified_at=rec["modified_at"],
                    upstream_modified_at=rec["modified_at"],
                    content_sha256=rec["content_sha256"],
                    transcript_chars=int(rec["transcript_chars"]),
                    archive_path=store.rel(path),
                    first_archived_at=_earliest_archived_at(store, mid) or rec.get("archived_at"),
                    deleted_upstream_at=rec.get("deleted_upstream_at"),
                    reappeared_at=rec.get("reappeared_at"),
                    attempts=0,
                )
                count += 1
            st.set_meta("rebuilt_at", now)
        st.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        st.close()
    backup: Path | None = None
    if state_path.exists():
        stamp = now.replace(":", "").replace("-", "")
        backup = state_path.with_name(f"{state_path.name}.bak-{stamp}")
        for suffix in ("", "-wal", "-shm"):
            side = Path(str(state_path) + suffix)
            if side.exists():
                shutil.move(side, Path(str(backup) + suffix))
    tmp.replace(state_path)
    return count, backup


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def status_report(state: State, store: ArchiveStore, *, now: datetime) -> dict[str, Any]:
    def age_h(key: str) -> float | None:
        value = state.get_meta(key)
        return None if value is None else round((now - parse_ts(value)).total_seconds() / 3600, 2)

    failed = [r.id for r in state.with_status("failed")]
    deleted = sum(1 for r in state.all() if r.deleted_upstream_at)
    usage = shutil.disk_usage(store.root if store.root.exists() else store.root.parent)
    return {
        "meetings_by_status": state.counts(),
        "deleted_upstream": deleted,
        "failed_ids": failed,
        "watermark": state.get_meta("watermark"),
        "last_ok_at": state.get_meta("last_ok_at"),
        "last_ok_age_h": age_h("last_ok_at"),
        "last_completed_at": state.get_meta("last_completed_at"),
        "last_completed_age_h": age_h("last_completed_at"),
        "last_published_at": state.get_meta("last_published_at"),
        "last_published_age_h": age_h("last_published_at"),
        "last_verified_remote_at": state.get_meta("last_verified_remote_at"),
        "archive_bytes": _dir_size(store.root),
        "disk_free_bytes": usage.free,
        "last_runs": [
            {k: r[k] for k in ("command", "started_at", "finished_at", "exit_code", "completed")}
            for r in state.last_runs(5)
        ],
    }
