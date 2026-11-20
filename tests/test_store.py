"""Unit tests: record/hash, Markdown, layout, guard, atomic writes, verify, state, lock."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.fake_wispr import FakeWisprServer, make_meeting
from tests.harness import make_env
from wispr_archiver.archive.model import build_record, content_hash, record_json
from wispr_archiver.archive.render import LEGACY_LABELS, render_markdown
from wispr_archiver.archive.store import (
    ArchiveStore,
    base_name,
    should_overwrite,
    slugify,
    snapshot_name,
    verify_archive,
    write_new_only,
)
from wispr_archiver.errors import LockBusy, StoreError
from wispr_archiver.lock import run_lock
from wispr_archiver.maintenance import rebuild_state, status_report
from wispr_archiver.state import State
from wispr_archiver.wispr.models import MeetingPayload, ParticipantsPayload

TZ = ZoneInfo("Europe/Paris")


def payload(**kw: Any) -> MeetingPayload:
    base: dict[str, Any] = {
        "id": "01234567-aaaa-4000-8000-000000000000",
        "title": "Test: café/1",
        "summary": "### Next Steps\n- a",
        "finalized": True,
        "has_transcript": True,
        "attendees": [],
        "todos": [],
        "start": "2001-02-03T09:00:00.000000Z",
        "end": "2001-02-03T09:30:00.000000Z",
        "modified_at": "2001-02-03T09:35:00.123456Z",
        "share_link": None,
    }
    base.update(kw)
    return MeetingPayload.model_validate(base)


def record(
    transcript: str = "Speaker A:  hi\n", *, archived_at: str = "t", **kw: Any
) -> dict[str, Any]:
    return build_record(
        meeting=payload(**kw),
        participants=None,
        transcript=transcript,
        notes="",
        archived_at=archived_at,
    )


# --- model ------------------------------------------------------------------------------


def test_hash_ignores_bookkeeping_fields() -> None:
    a = record(archived_at="t1")
    b = record(archived_at="t2")
    assert a["content_sha256"] == b["content_sha256"]
    b["deleted_upstream_at"] = "2026-11-20T00:00:00Z"
    b["archive_path"] = "x"
    assert content_hash(b) == a["content_sha256"]


def test_hash_covers_content_and_unknown_fields() -> None:
    a = record()
    assert record("other")["content_sha256"] != a["content_sha256"]
    assert record(summary="changed")["content_sha256"] != a["content_sha256"]
    with_extra = record(new_upstream_field={"x": 1})
    assert with_extra["upstream_extra"] == {"new_upstream_field": {"x": 1}}
    assert with_extra["content_sha256"] != a["content_sha256"]


def test_promoted_fields_and_participants_meta() -> None:
    p = ParticipantsPayload.model_validate(
        {"meeting_id": "x", "participants": [{"name": "A"}], "user": {"name": "U"}, "zz": 1}
    )
    rec = build_record(
        meeting=payload(shared=True, folders=["f"]),
        participants=p,
        transcript="",
        notes="",
        archived_at="t",
    )
    assert (rec["shared"], rec["folders"], rec["upstream_extra"]) == (True, ["f"], {})
    assert rec["participants_meta"]["user"] == {"name": "U"}
    assert rec["participants_meta"]["zz"] == 1


def test_record_json_is_stable_utf8() -> None:
    js = record_json(record())
    assert "Test: café/1" in js  # not \\u-escaped
    assert js.endswith("\n")
    assert record_json(record()) == js


# --- layout -----------------------------------------------------------------------------


def test_base_name_matches_prototype() -> None:
    assert base_name(record()) == "20010203T0900_test-café-1__01234567"


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        (None, "untitled"),
        ("", "untitled"),
        ("!!!", "untitled"),
        ("A_B  c", "a-b-c"),
        ("Éclair", "éclair"),
        ("x" * 80, "x" * 60),
    ],
)
def test_slugify(title: str | None, slug: str) -> None:
    assert slugify(title) == slug


@given(st.text(max_size=200))
def test_slug_is_filename_safe(title: str) -> None:
    s = slugify(title)
    assert s
    assert "/" not in s
    assert "\x00" not in s
    assert len(s.encode()) <= 240


def test_snapshot_name_has_no_colons() -> None:
    rec = record()
    name = snapshot_name(rec)
    assert ":" not in name
    assert name == f"20010203T093500.123456Z__{rec['content_sha256'][:8]}.json"


# --- guard ------------------------------------------------------------------------------


def test_guard_rules() -> None:
    prev = record("x" * 1000)
    assert should_overwrite(None, prev, shrink_threshold=0.1).reason == "new"
    assert (
        should_overwrite(prev, record("x" * 1000, archived_at="t9"), shrink_threshold=0.1).reason
        == "unchanged"
    )
    assert (
        should_overwrite(prev, record(""), shrink_threshold=0.1).reason
        == "upstream_transcript_removed"
    )
    assert (
        should_overwrite(prev, record("x" * 899), shrink_threshold=0.1).reason
        == "upstream_transcript_shrunk"
    )
    assert should_overwrite(prev, record("x" * 900), shrink_threshold=0.1).write
    assert should_overwrite(prev, record("x" * 5000), shrink_threshold=0.1).write
    empty = record("")
    assert should_overwrite(empty, record("", summary="new"), shrink_threshold=0.1).write


@given(st.integers(0, 10_000), st.integers(0, 10_000))
def test_guard_never_accepts_big_shrink(prev_len: int, new_len: int) -> None:
    prev = record("p" * prev_len)
    new = record("n" * new_len)
    d = should_overwrite(prev, new, shrink_threshold=0.1)
    if d.write:
        assert new_len >= prev_len * 0.9
        assert not (prev_len > 0 and new_len == 0)


# --- store ------------------------------------------------------------------------------


def store(tmp_path: Path, **kw: Any) -> ArchiveStore:
    return ArchiveStore(tmp_path / "a", TZ, shrink_threshold=0.1, **kw)


def test_write_new_only_never_overwrites(tmp_path: Path) -> None:
    f = tmp_path / "d" / "x.json"
    assert write_new_only(f, b"one")
    assert not write_new_only(f, b"two")
    assert f.read_bytes() == b"one"
    assert [p.name for p in f.parent.iterdir()] == ["x.json"]  # no temp leftovers


def test_archive_files_are_private_regardless_of_umask(tmp_path: Path) -> None:
    old = os.umask(0)  # most permissive: the store itself must restrict access
    try:
        s = store(tmp_path)
        s.save(record())
        s.save(record("Speaker A:  hi there, longer\n"))  # atomic replace keeps the mode
    finally:
        os.umask(old)
    root = tmp_path / "a"
    files = [p for p in root.rglob("*") if p.is_file()]
    dirs = [root, *(p for p in root.rglob("*") if p.is_dir())]
    assert len(files) == 4  # json + md + 2 snapshots
    assert {oct(p.stat().st_mode & 0o777) for p in files} == {"0o640"}
    assert {oct(p.stat().st_mode & 0o777) for p in dirs} == {"0o750"}


def test_save_new_then_unchanged_then_updated(tmp_path: Path) -> None:
    s = store(tmp_path)
    r1 = s.save(record())
    assert (r1.action, r1.snapshot_written) == ("new", True)
    assert r1.archive_path.startswith("meetings/2001/02/")
    r2 = s.save(record(archived_at="later"))
    assert (r2.action, r2.bytes_written) == ("unchanged", 0)
    r3 = s.save(record("Speaker A:  hi there, longer\n"))
    assert r3.action == "updated"
    assert len(list(s.iter_snapshots())) == 2


def test_snapshot_clash_is_an_error(tmp_path: Path) -> None:
    s = store(tmp_path)
    rec = record()
    s.save(rec)
    (snap,) = s.iter_snapshots()
    data = json.loads(snap.read_text())
    data["content_sha256"] = "0" * 64
    snap.write_text(json.dumps(data))
    with pytest.raises(StoreError):
        s._write_snapshot(rec)


def test_id8_collision_uses_full_id(tmp_path: Path) -> None:
    s = store(tmp_path)
    a = s.save(record())
    b = s.save(record(id="01234567-bbbb-4000-8000-000000000000"))
    assert a.archive_path != b.archive_path
    assert b.archive_path.endswith("__01234567-bbbb-4000-8000-000000000000.json")
    assert s.locate("01234567-bbbb-4000-8000-000000000000") is not None


def test_dry_run_store_writes_nothing(tmp_path: Path) -> None:
    s = store(tmp_path, dry_run=True)
    out = s.save(record())
    assert out.action == "new"
    assert out.bytes_written > 0
    assert not (tmp_path / "a").exists()


def test_markdown_render(tmp_path: Path) -> None:
    rec = record()
    rec["archive_path"] = "p"
    md = render_markdown(rec, TZ)
    assert md.startswith('---\nschema: "wispr-archive/v1"\n')
    assert 'start_local: "2001-02-03 10:00 (Europe/Paris)"' in md
    assert "- [ ] a" in md
    assert "## Transcript\n\nSpeaker A:  hi\n" in md


# --- verify -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verify_detects_tampering(tmp_path: Path) -> None:
    env = make_env(tmp_path, FakeWisprServer([make_meeting(1), make_meeting(2)]))
    await env.run("backfill")
    s = env.store()
    assert verify_archive(s).ok
    mid = make_meeting(1).id
    path = s.locate(mid)
    assert path is not None
    rec = json.loads(path.read_text())
    rec["transcript_text"] = rec["transcript_text"][:-5]
    path.write_text(json.dumps(rec))
    problems = {p for _, p in verify_archive(s).problems or []}
    assert {
        "hash_mismatch",
        "transcript_length_mismatch",
        "snapshot_missing",
    } <= problems


def test_existing_markdown_labels_preserve_idempotence(tmp_path: Path) -> None:
    s = store(tmp_path)
    out = s.save(record(title="", transcript=""))
    path = (s.root / out.archive_path).with_suffix(".md")
    old = render_markdown(out.record, TZ, LEGACY_LABELS).encode()
    path.write_bytes(old)
    assert verify_archive(s).ok
    assert s.save(record(title="", transcript="")).bytes_written == 0
    assert path.read_bytes() == old


def test_verify_requires_archive_layout(tmp_path: Path) -> None:
    s = store(tmp_path)
    assert not verify_archive(s).ok
    s.root.mkdir()
    assert not verify_archive(s).ok


def test_store_rejects_nested_meeting_id(tmp_path: Path) -> None:
    s = store(tmp_path)
    with pytest.raises(StoreError):
        s.save(record(id="group/meeting"))
    assert not s.root.exists()


def test_retry_repairs_markdown_without_rewriting_json(tmp_path: Path) -> None:
    s = store(tmp_path)
    saved = s.save(record())
    path = s.root / saved.archive_path
    json_before = path.read_bytes()
    md = path.with_suffix(".md")
    expected = md.read_bytes()
    md.write_bytes(expected.split(b"## Transcript")[0])
    assert not verify_archive(s).ok
    assert s.save(record(archived_at="later")).bytes_written == len(expected)
    assert md.read_bytes() == expected
    md.unlink()
    assert s.save(record(archived_at="later")).bytes_written == len(expected)
    assert path.read_bytes() == json_before
    assert s.save(record()).bytes_written == 0
    assert verify_archive(s).ok


# --- state / rebuild --------------------------------------------------------------------


@pytest.mark.anyio
async def test_rebuild_state_restores_rows_and_keeps_backup(tmp_path: Path) -> None:
    server = FakeWisprServer([make_meeting(1), make_meeting(2, days_ago=3)])
    env = make_env(tmp_path, server)
    await env.run("backfill")
    server.delete(make_meeting(2).id)
    await env.run("reconcile")
    before = {
        r.id: (r.content_sha256, r.archive_path, r.deleted_upstream_at) for r in env.state().all()
    }
    count, backup = rebuild_state(env.store(), env.settings.state_path, now="2026-11-19T00:00:00Z")
    assert count == 2
    assert backup is not None
    assert backup.exists()
    after = {
        r.id: (r.content_sha256, r.archive_path, r.deleted_upstream_at) for r in env.state().all()
    }
    assert after == before
    digest = env.tree_digest()
    _, s = await env.run("sync")
    assert s.bytes_written == 0
    assert env.tree_digest() == digest


def test_state_attempts_and_reset(tmp_path: Path) -> None:
    st = State.open(tmp_path / "s.db")
    assert st.record_failure("m", kind="transient", failed=False) == 1
    assert st.record_failure("m", kind="transient", failed=True) == 2
    assert st.get("m").status == "failed"  # type: ignore[union-attr]
    st.mark_archived(
        "m",
        status="ok",
        modified_at="2026-11-18T00:00:00Z",
        content_sha256="a",
        transcript_chars=1,
        archive_path="p",
        at="t",
    )
    row = st.get("m")
    assert row is not None
    assert (row.status, row.attempts, row.last_error) == ("ok", 0, None)
    st.close()


def test_state_dry_run_never_touches_file(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    real = State.open(path)
    real.set_meta("watermark", "w1")
    real.close()
    mtime = path.stat().st_mtime_ns
    dry = State.open(path, dry_run=True)
    assert dry.get_meta("watermark") == "w1"
    dry.set_meta("watermark", "w2")
    dry.close()
    assert path.stat().st_mtime_ns == mtime
    again = State.open(path)
    assert again.get_meta("watermark") == "w1"
    again.close()


def test_status_report(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    st = State.open(tmp_path / "s.db")
    st.set_meta("last_ok_at", "2026-11-18T10:00:00Z")
    st.set_meta("last_published_at", "2026-11-18T11:30:00Z")
    st.record_failure("bad", kind="tool_error", failed=True)
    rep = status_report(st, store(tmp_path), now=datetime(2026, 11, 18, 12, tzinfo=UTC))
    assert rep["failed_ids"] == ["bad"]
    assert rep["last_ok_age_h"] == 2.0
    assert rep["last_published_age_h"] == 0.5
    assert rep["last_verified_remote_at"] is None
    st.close()


# --- lock -------------------------------------------------------------------------------


def test_run_lock_is_exclusive(tmp_path: Path) -> None:
    lock = tmp_path / "new-dir" / "run.lock"
    with run_lock(lock):
        assert oct(lock.stat().st_mode & 0o777) == "0o600"
        assert oct(lock.parent.stat().st_mode & 0o777) == "0o700"  # created private
        with pytest.raises(LockBusy), run_lock(lock):
            pass
    with run_lock(lock):  # released
        pass
