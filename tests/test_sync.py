"""Engine scenarios against FakeWisprServer (M1 acceptance list)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from tests.fake_wispr import FakeWisprServer, error_result, make_meeting
from tests.harness import Env, make_env
from tests.wispr_format import HEADER, transcript_text
from wispr_archiver.errors import ExitCode, ReauthRequired, ToolError
from wispr_archiver.log import setup_logging

pytestmark = pytest.mark.anyio


def three() -> FakeWisprServer:
    return FakeWisprServer(
        [
            make_meeting(1, days_ago=10, transcript_len=50_000),
            make_meeting(2, days_ago=5, content="# Notes\n" + "- note item\n" * 4000),
            make_meeting(3, days_ago=1, shared=True),
        ]
    )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path, three())


# --- backfill ---------------------------------------------------------------------------


async def test_backfill_archives_everything(env: Env) -> None:
    code, s = await env.run("backfill")
    assert code == ExitCode.OK
    assert (s.listed, s.new, s.snapshots, s.failed) == (3, 3, 3, 0)
    m1 = env.server.meetings[make_meeting(1).id]
    rec = env.main_json(m1.id)
    assert rec["schema"] == "wispr-archive/v1"
    assert rec["transcript_text"] == m1.transcript
    assert rec["transcript_chars"] == 50_000
    assert rec["action_items"][0] == {"source": "summary", "owner": "Ivan", "text": "task 1"}
    assert [p["name"] for p in rec["participants"]] == ["Owner Name", "Guest 1"]
    assert rec["archive_path"].startswith("meetings/2026/11/20261108T1000_synthetic-meeting-1__")
    md = (env.archive / rec["archive_path"]).with_suffix(".md").read_text(encoding="utf-8")
    assert "## Transcript" in md
    assert m1.transcript in md
    assert "start_local" in md
    assert "Europe/Paris" in md
    m2 = env.server.meetings[make_meeting(2).id]
    assert env.main_json(m2.id)["notes_md"] == m2.content
    # shared meeting: no participants call
    shared_id = make_meeting(3).id
    calls = [a for n, a in env.server.calls if n == "get_meeting_participants_enriched"]
    assert all(a["meeting_id"] != shared_id for a in calls)
    assert env.main_json(shared_id)["participants"] == []
    assert env.meta("watermark") is not None


async def test_chunking_requests_are_40000(env: Env) -> None:
    await env.run("backfill")
    views = [
        a["view_transcript"]
        for n, a in env.server.calls
        if n == "get_meeting" and "view_transcript" in a and a["meeting_id"] == make_meeting(1).id
    ]
    assert views == [
        {"start_char": 0, "char_limit": 40_000},
        {"start_char": 40_000, "char_limit": 40_000},
    ]


async def test_rate_limit_one_request_per_second(env: Env) -> None:
    await env.run("backfill")
    # throttle sleeps keep calls >= 1 s apart; total time >= calls - 1
    assert env.clock.t >= len(env.server.calls) - 1


# --- idempotency ------------------------------------------------------------------------


async def test_second_run_writes_zero_bytes(env: Env) -> None:
    await env.run("backfill")
    before = env.tree_digest()
    code, s = await env.run("backfill")
    assert code == ExitCode.OK
    assert (s.new, s.updated, s.bytes_written, s.snapshots) == (0, 0, 0, 0)
    assert s.skipped == 3
    assert env.tree_digest() == before
    n_get = env.server.count("get_meeting")
    code, s = await env.run("sync")
    assert (code, s.bytes_written) == (ExitCode.OK, 0)
    assert env.server.count("get_meeting") == n_get  # nothing fetched
    assert env.tree_digest() == before


async def test_refetch_same_content_is_unchanged(env: Env) -> None:
    await env.run("backfill")
    before = env.tree_digest()
    st = env.state()
    st.upsert(make_meeting(2).id, modified_at="2000-01-01T00:00:00Z")  # force a refetch
    st.close()
    code, s = await env.run("backfill")  # full listing reaches the old meeting
    assert (code, s.unchanged, s.bytes_written) == (ExitCode.OK, 1, 0)
    assert env.tree_digest() == before


# --- incremental ------------------------------------------------------------------------


async def test_incremental_sync_new_and_changed(tmp_path: Path) -> None:
    server = FakeWisprServer([make_meeting(i, days_ago=30 - i) for i in range(1, 11)])
    env = make_env(tmp_path, server, search_limit=2)
    await env.run("backfill")
    pages_backfill = server.count("search_meetings")
    target = make_meeting(9).id
    server.touch(target, summary="### Next Steps\n- (Anna) new item")
    server.add(make_meeting(11, days_ago=0))
    server.calls.clear()
    code, s = await env.run("sync")
    assert code == ExitCode.OK
    assert (s.new, s.updated) == (1, 1)
    assert server.count("search_meetings") < pages_backfill  # stopped at watermark - 24h
    fetched = {a["meeting_id"] for n, a in server.calls if n == "get_meeting"}
    assert fetched == {target, make_meeting(11).id}
    assert env.main_json(target)["action_items"] == [
        {"source": "summary", "owner": "Anna", "text": "new item"}
    ]


async def test_change_creates_snapshot_and_keeps_old_one(env: Env) -> None:
    await env.run("backfill")
    mid = make_meeting(2).id
    (old_snap,) = env.snapshots(mid)
    old_bytes = old_snap.read_bytes()
    env.server.touch(mid, summary="edited summary")
    code, s = await env.run("sync")
    assert (code, s.updated, s.snapshots) == (ExitCode.OK, 1, 1)
    snaps = env.snapshots(mid)
    assert len(snaps) == 2
    assert old_snap.read_bytes() == old_bytes  # never overwritten
    assert env.main_json(mid)["summary_md"] == "edited summary"


async def test_rename_keeps_archive_path(env: Env) -> None:
    await env.run("backfill")
    mid = make_meeting(1).id
    path = env.main_json(mid)["archive_path"]
    env.server.touch(mid, title="A different title")
    await env.run("sync")
    rec = env.main_json(mid)
    assert rec["archive_path"] == path
    assert rec["title"] == "A different title"
    assert len(list((env.archive / "meetings").rglob("*.json"))) == 3


# --- guard ------------------------------------------------------------------------------


async def test_transcript_removed_is_guarded(env: Env) -> None:
    await env.run("backfill")
    mid = make_meeting(1).id
    original = env.main_json(mid)
    env.server.touch(mid, transcript="", has_transcript=False)
    code, s = await env.run("sync")
    assert (code, s.guarded, s.updated) == (ExitCode.OK, 1, 0)
    rec = env.main_json(mid)
    assert rec["transcript_text"] == original["transcript_text"]
    assert rec["content_sha256"] == original["content_sha256"]
    assert len(env.snapshots(mid)) == 2  # the poorer version is still recorded
    assert env.row(mid).status == "upstream_transcript_removed"
    # no refetch loop on the next run
    n = env.server.count("get_meeting")
    await env.run("sync")
    assert env.server.count("get_meeting") == n


@pytest.mark.parametrize(("new_len", "outcome"), [(44_999, "guarded"), (45_000, "updated")])
async def test_shrink_threshold(env: Env, new_len: int, outcome: str) -> None:
    await env.run("backfill")
    mid = make_meeting(1).id  # 50,000 chars; a 10% reduction leaves 45,000
    env.server.touch(mid, transcript=transcript_text(new_len))
    _, s = await env.run("sync")
    assert getattr(s, outcome) == 1
    if outcome == "guarded":
        assert env.row(mid).status == "upstream_transcript_shrunk"
        assert env.main_json(mid)["transcript_chars"] == 50_000


async def test_transcript_restored_after_guard(env: Env) -> None:
    await env.run("backfill")
    mid = make_meeting(1).id
    env.server.touch(mid, transcript="", has_transcript=False)
    await env.run("sync")
    env.server.touch(mid, transcript=transcript_text(60_000), has_transcript=True)
    _, s = await env.run("sync")
    assert s.updated == 1
    assert env.row(mid).status == "ok"


# --- deletion upstream ------------------------------------------------------------------


async def test_upstream_deletion_marks_and_keeps_files(env: Env) -> None:
    await env.run("backfill")
    mid = make_meeting(2).id
    path = env.archive / env.main_json(mid)["archive_path"]
    env.server.delete(mid)
    code, s = await env.run("reconcile")
    assert (code, s.deleted_marked) == (ExitCode.OK, 1)
    assert path.is_file()
    assert path.with_suffix(".md").is_file()
    rec = env.main_json(mid)
    assert rec["deleted_upstream_at"]
    assert rec["transcript_text"]
    assert "Meeting removed from Wispr" in path.with_suffix(".md").read_text(encoding="utf-8")
    assert env.row(mid).deleted_upstream_at
    # sync never marks deletions and never deletes
    code, s = await env.run("sync")
    assert (code, s.deleted_marked) == (ExitCode.OK, 0)
    assert path.is_file()


async def test_reappeared_meeting_is_unmarked(env: Env) -> None:
    await env.run("backfill")
    mid = make_meeting(2).id
    saved = env.server.meetings[mid]
    env.server.delete(mid)
    await env.run("reconcile")
    env.server.add(saved)
    _, s = await env.run("reconcile")
    assert s.reappeared == 1
    rec = env.main_json(mid)
    assert rec["deleted_upstream_at"] is None
    assert rec["reappeared_at"]


async def test_reconcile_refuses_mass_deletion(tmp_path: Path) -> None:
    server = FakeWisprServer([make_meeting(i, days_ago=i) for i in range(1, 8)])
    env = make_env(tmp_path, server)
    await env.run("backfill")
    for i in range(1, 7):
        server.delete(make_meeting(i).id)
    code, s = await env.run("reconcile")
    assert (code, s.deleted_marked, s.blocked) == (ExitCode.PARTIAL, 0, 1)
    assert any("Reconcile skipped deletion markers" in t for t in env.notifier.sent)
    assert env.main_json(make_meeting(1).id)["deleted_upstream_at"] is None


async def test_reconcile_refuses_empty_listing(env: Env) -> None:
    await env.run("backfill")
    env.server.meetings.clear()
    code, s = await env.run("reconcile")
    assert (code, s.deleted_marked) == (ExitCode.PARTIAL, 0)


# --- failures, watermark ----------------------------------------------------------------


async def test_failure_keeps_watermark_then_recovers(env: Env) -> None:
    await env.run("backfill")
    wm = env.meta("watermark")
    env.server.add(make_meeting(4, days_ago=0))
    bad = make_meeting(4).id

    def hook(name: str, args: dict[str, Any], n: int) -> Any:
        if name == "get_meeting" and args["meeting_id"] == bad:
            return error_result("Something unexpected happened")
        return None

    env.server.hook = hook
    code, s = await env.run("sync")
    assert (code, s.failed) == (ExitCode.PARTIAL, 1)
    assert env.meta("watermark") == wm
    row = env.row(bad)
    assert (row.status, row.attempts) == ("failed", 5)
    assert any(bad in t for t in env.notifier.sent)
    env.server.hook = None
    code, s = await env.run("sync")
    assert (code, s.new) == (ExitCode.OK, 1)
    assert env.row(bad).status == "ok"
    assert env.row(bad).attempts == 0
    assert env.meta("watermark") != wm


async def test_unchanged_watermark_is_not_logged_as_moved(env: Env) -> None:
    buf = io.StringIO()
    setup_logging("INFO", stream=buf)
    await env.run("backfill")
    first = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
    assert first.count("watermark_moved") == 1
    buf.truncate(0)
    buf.seek(0)
    await env.run("sync")
    second = [json.loads(line)["event"] for line in buf.getvalue().splitlines()]
    assert "watermark_moved" not in second
    assert "run_finished" in second


async def test_crash_mid_run_keeps_watermark(env: Env) -> None:
    await env.run("backfill")
    wm = env.meta("watermark")
    env.server.add(make_meeting(4, days_ago=0))
    env.server.add(make_meeting(5, days_ago=0))

    class Killed(BaseException):
        pass

    def hook(name: str, args: dict[str, Any], n: int) -> Any:
        if name == "get_meeting" and args["meeting_id"] == make_meeting(5).id:
            return Killed()
        return None

    env.server.hook = hook
    with pytest.raises(Killed):
        await env.run("sync")
    assert env.meta("watermark") == wm
    env.server.hook = None
    code, s = await env.run("sync")
    assert code == ExitCode.OK
    assert env.row(make_meeting(5).id).status == "ok"
    before = env.tree_digest()
    _, s = await env.run("sync")
    assert s.bytes_written == 0
    assert env.tree_digest() == before


async def test_retry_finishes_interrupted_markdown_write(tmp_path: Path, monkeypatch) -> None:
    from wispr_archiver.archive import store
    from wispr_archiver.archive.store import verify_archive

    env = make_env(tmp_path, FakeWisprServer([make_meeting(1)]))
    write = store.atomic_write

    def fail_markdown(path: Path, data: bytes) -> int:
        if path.suffix == ".md":
            raise OSError("simulated write failure")
        return write(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(store, "atomic_write", fail_markdown)
        assert (await env.run("backfill"))[0] == ExitCode.PARTIAL
    (main,) = env.store().iter_main()
    (snapshot,) = env.store().iter_snapshots()
    before = (main.read_bytes(), snapshot.read_bytes())
    assert not main.with_suffix(".md").exists()
    code, summary = await env.run("sync")
    assert code == ExitCode.OK
    assert summary.bytes_written == main.with_suffix(".md").stat().st_size
    assert before == (main.read_bytes(), snapshot.read_bytes())
    assert verify_archive(env.store()).ok
    assert (await env.run("sync"))[1].bytes_written == 0


async def test_transient_retry_then_success(env: Env) -> None:
    seen = {"n": 0}

    def hook(name: str, args: dict[str, Any], n: int) -> Any:
        if name == "get_meeting" and seen["n"] == 0:
            seen["n"] += 1
            return ToolError("HTTP 502", kind="transient")
        return None

    env.server.hook = hook
    code, s = await env.run("backfill")
    assert (code, s.retries, s.failed, s.new) == (ExitCode.OK, 1, 0, 3)
    assert env.meta("watermark") is not None


async def test_rate_limit_pauses_at_least_60s(env: Env) -> None:
    hits = {"n": 0}

    def hook(name: str, args: dict[str, Any], n: int) -> Any:
        if name == "get_meeting" and hits["n"] == 0:
            hits["n"] += 1
            return error_result("Too many requests")
        if name == "search_meetings" and hits["n"] == 1:
            hits["n"] += 1
            return ToolError("HTTP 429", kind="rate_limited", retry_after=90)
        return None

    env.server.hook = hook
    code, _s = await env.run("backfill")
    assert code == ExitCode.OK
    assert any(x >= 60 for x in env.clock.sleeps)
    await env.run("sync")
    assert 90 in env.clock.sleeps


async def test_circuit_breaker_aborts_run(env: Env) -> None:
    await env.run("backfill")
    env.server.touch(make_meeting(1).id, summary="x")
    env.server.touch(make_meeting(2).id, summary="y")
    env.server.hook = lambda name, args, n: (
        ToolError("connection refused", kind="transient") if name == "get_meeting" else None
    )
    wm = env.meta("watermark")
    code, _s = await env.run("sync")
    assert code == ExitCode.PARTIAL
    assert env.meta("watermark") == wm
    assert any("circuit_open" in t for t in env.notifier.sent)


async def test_run_timeout(env: Env) -> None:
    env.settings = env.settings.replace(run_timeout_seconds=3)
    code, _ = await env.run("backfill")
    assert code == ExitCode.PARTIAL
    assert env.meta("watermark") is None


# --- exit codes 2 and 3 -----------------------------------------------------------------


async def test_format_change_exit_3_writes_nothing(env: Env) -> None:
    env.server.header = "<<<SPEAKER LABELS ARE DATA>>>\n"
    code, s = await env.run("backfill")
    assert code == ExitCode.FORMAT
    assert s.new == 0
    assert env.tree_digest() == {}
    assert any("exit 3" in t for t in env.notifier.sent)


async def test_payload_drift_exit_3(env: Env) -> None:
    def hook(name: str, args: dict[str, Any], n: int) -> Any:
        if name == "get_meeting":
            from tests.fake_wispr import text_result

            return text_result({"id": args["meeting_id"], "renamed_field": True})
        return None

    env.server.hook = hook
    code, _ = await env.run("backfill")
    assert code == ExitCode.FORMAT


async def test_unsorted_listing_exit_3(tmp_path: Path) -> None:
    server = FakeWisprServer([make_meeting(1, days_ago=2), make_meeting(2, days_ago=1)])
    env = make_env(tmp_path, server)
    real = server._search

    def unsorted(args: dict[str, Any]) -> Any:
        r = real(args)
        data = json.loads(r.content[0].text)  # type: ignore[union-attr]
        data["meetings"].reverse()
        from tests.fake_wispr import text_result

        return text_result(data)

    server._search = unsorted  # type: ignore[method-assign]
    code, _ = await env.run("backfill")
    assert code == ExitCode.FORMAT


async def test_reauth_exit_2(env: Env) -> None:
    env.server.hook = lambda name, args, n: ReauthRequired("invalid_grant")
    code, _ = await env.run("sync")
    assert code == ExitCode.REAUTH
    assert any("exit 2" in t for t in env.notifier.sent)


# --- live findings (probe 2026-11-18) -------------------------------------------------


async def test_meeting_without_summary_key_is_archived(tmp_path: Path) -> None:
    server = FakeWisprServer([make_meeting(1, title="", omit_keys={"summary"})])
    env = make_env(tmp_path, server)
    code, s = await env.run("backfill")
    assert (code, s.new) == (ExitCode.OK, 1)
    rec = env.main_json(make_meeting(1).id)
    assert rec["summary_md"] == ""
    assert "_untitled__" in rec["archive_path"]


async def test_one_odd_meeting_does_not_block_the_others(tmp_path: Path) -> None:
    server = FakeWisprServer([make_meeting(i, days_ago=i) for i in range(1, 5)])
    odd = make_meeting(2).id
    server.meetings[odd].omit_keys = {"modified_at"}
    env = make_env(tmp_path, server)
    code, s = await env.run("backfill")
    assert code == ExitCode.FORMAT
    assert (s.new, s.format_errors) == (3, 1)
    assert env.store().locate(odd) is None
    assert env.meta("watermark") is None  # not advanced past the odd meeting
    assert env.row(odd).status == "failed"
    assert sum(odd in t and "exit 3" in t for t in env.notifier.sent) == 1
    assert not any("could not be archived" in t for t in env.notifier.sent)
    server.meetings[odd].omit_keys = set()
    code, s = await env.run("sync")
    assert (code, s.new) == (ExitCode.OK, 1)
    assert env.meta("watermark") is not None


async def test_listing_format_error_still_stops_the_run(env: Env) -> None:
    def hook(name: str, args: dict[str, Any], n: int) -> Any:
        if name == "search_meetings":
            from tests.fake_wispr import text_result

            return text_result({"meetings": "not a list", "has_more": False})
        return None

    env.server.hook = hook
    code, s = await env.run("backfill")
    assert code == ExitCode.FORMAT
    assert s.listed == 0


# --- pending / finalized ----------------------------------------------------------------


async def test_pending_meeting_is_rechecked_until_finalized(env: Env) -> None:
    env.server.add(make_meeting(4, days_ago=0, finalized=False))
    mid = make_meeting(4).id
    code, s = await env.run("backfill")
    assert (code, s.pending, s.new) == (ExitCode.OK, 1, 3)
    assert env.store().locate(mid) is None
    assert env.row(mid).status == "pending"
    wm = env.meta("watermark")
    assert wm is not None
    assert wm <= env.server.meetings[mid].modified_at
    # finalize WITHOUT bumping modified_at ([?] live check #9) and push it below the overlap
    env.server.meetings[mid].finalized = True
    for i in (1, 2, 3):
        env.server.touch(make_meeting(i).id, summary=f"bump {i}")
    env.clock.advance(days=3)
    code, s = await env.run("sync")
    assert code == ExitCode.OK
    assert env.row(mid).status == "ok"
    assert env.store().locate(mid) is not None


async def test_error_rows_outside_listing_are_retried(tmp_path: Path) -> None:
    server = FakeWisprServer([make_meeting(i, days_ago=20 - i) for i in range(1, 6)])
    env = make_env(tmp_path, server)
    await env.run("backfill")
    old = make_meeting(1).id  # far below watermark - 24h
    st = env.state()
    st.upsert(old, status="error", attempts=2)
    st.close()
    server.calls.clear()
    code, s = await env.run("sync")
    assert code == ExitCode.OK
    assert any(n == "get_meeting" and a["meeting_id"] == old for n, a in server.calls)
    assert env.row(old).status == "ok"
    assert s.unchanged == 1


# --- truncated listing ------------------------------------------------------------------


async def test_truncated_listing_is_split_by_windows(tmp_path: Path) -> None:
    server = FakeWisprServer([make_meeting(i, days_ago=i * 3) for i in range(1, 8)], truncate_at=3)
    env = make_env(tmp_path, server, search_limit=2)
    code, s = await env.run("backfill")
    assert (code, s.listed, s.new) == (ExitCode.OK, 7, 7)
    assert any("since" in a for n, a in server.calls if n == "search_meetings")


# --- utf-16 counting server -------------------------------------------------------------


async def test_utf16_counting_server_with_emoji(tmp_path: Path) -> None:
    text = "Speaker A:  emoji 😀🎉 in text\n" * 3000
    server = FakeWisprServer([make_meeting(1, transcript=text)], unit="utf16")
    env = make_env(tmp_path, server)
    code, _ = await env.run("backfill")
    assert code == ExitCode.OK
    assert env.main_json(make_meeting(1).id)["transcript_text"] == text


# --- dry run ----------------------------------------------------------------------------


async def test_dry_run_writes_nothing(env: Env) -> None:
    code, s = await env.run("backfill", dry_run=True)
    assert (code, s.new) == (ExitCode.OK, 3)
    assert s.bytes_written > 0  # what would be written
    assert env.tree_digest() == {}
    assert not env.settings.state_path.exists()


# --- alerts -----------------------------------------------------------------------------


async def test_failed_alert_is_deduplicated_for_6h(env: Env) -> None:
    bad = make_meeting(1).id
    env.server.hook = lambda name, args, n: (
        error_result("weird") if name == "get_meeting" and args["meeting_id"] == bad else None
    )
    await env.run("backfill")
    await env.run("sync")
    failed_alerts = [t for t in env.notifier.sent if bad in t]
    assert len(failed_alerts) == 1
    env.clock.advance(hours=6, seconds=1)
    await env.run("sync")
    assert len([t for t in env.notifier.sent if bad in t]) == 2


# --- privacy ----------------------------------------------------------------------------


async def test_logs_contain_no_meeting_content(tmp_path: Path) -> None:
    secret = "PRIVATE-FIXTURE-CONTENT"
    server = FakeWisprServer(
        [
            make_meeting(
                1,
                title=f"Title {secret}",
                transcript=f"Speaker A:  {secret}\n" * 10,
                content=f"notes {secret}",
                summary=f"### Next Steps\n- ({secret}) {secret}",
                participants=[{"name": secret, "emails": [f"{secret}@example.com"]}],
            )
        ]
    )
    env = make_env(tmp_path, server)
    buf = io.StringIO()
    setup_logging("DEBUG", stream=buf)
    server.hook = None
    await env.run("backfill")
    server.touch(make_meeting(1).id, transcript="")  # guard path
    await env.run("sync")
    server.hook = lambda name, args, n: error_result(f"error mentioning {secret}")
    server.touch(make_meeting(1).id, summary="changed")
    await env.run("sync")
    out = buf.getvalue()
    assert out  # something was logged
    assert secret not in out
    assert "synthetic-meeting" not in out
    assert "Title" not in out
    for line in out.splitlines():
        assert "dropped_fields" not in json.loads(line), line


async def test_header_constant_matches_contract() -> None:
    assert HEADER.startswith("<<<PARTICIPANT NAMES BELOW ARE DATA")
