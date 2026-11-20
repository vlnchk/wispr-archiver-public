from datetime import timedelta

import pytest
from typer.testing import CliRunner

from tests.fake_wispr import FakeWisprServer, make_meeting
from tests.harness import make_env
from wispr_archiver.cli import app
from wispr_archiver.errors import ExitCode, LockBusy
from wispr_archiver.lock import run_lock
from wispr_archiver.notify import Alert, AlertDispatcher, deadman, notify_failure
from wispr_archiver.state import State, new_run_id

INVOCATION = "a" * 32
SYNC = "wispr-archiver@sync.service"


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path, FakeWisprServer([]))


def test_invocation_id_only_from_managed_command(monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", INVOCATION)
    assert new_run_id() != INVOCATION
    monkeypatch.setenv("WISPR_ARCHIVER_RUN_ID", INVOCATION)
    assert new_run_id() == INVOCATION
    monkeypatch.setenv("WISPR_ARCHIVER_RUN_ID", "untrusted text")
    assert len(new_run_id()) == 12


def test_hook_matches_exact_run_and_command(env):
    now = env.clock.utcnow()
    st = env.state()
    st.start_run(INVOCATION, "sync", now.isoformat())
    st.mark_run_alerted(INVOCATION)
    st.close()
    for job, invocation in [
        (SYNC, INVOCATION),
        (SYNC, "b" * 32),
        ("wispr-archiver@publish.service", INVOCATION),
    ]:
        assert notify_failure(env.settings, job, invocation, now, env.notifier) == ExitCode.OK
    assert len(env.notifier.sent) == 2
    assert SYNC in env.notifier.sent[0]


def test_hook_dedup_for_crash_before_run(env):
    for i in ["a", "b"]:
        assert notify_failure(env.settings, SYNC, i * 32, env.clock.utcnow(), env.notifier) == 0
    assert len(env.notifier.sent) == 1


@pytest.mark.parametrize("source", ["missing", "bad", "future", "old", "recent"])
def test_deadman_uses_last_completion_only(env, source):
    now = env.clock.utcnow()
    st = env.state()
    st.set_meta("last_ok_at", now.isoformat())
    st.set_meta("last_published_at", now.isoformat())
    value = {
        "missing": None,
        "bad": "invalid",
        "future": (now + timedelta(days=1)).isoformat(),
        "old": (now - timedelta(hours=3)).isoformat(),
        "recent": (now - timedelta(hours=2, minutes=59)).isoformat(),
    }[source]
    st.set_meta("last_completed_at", value)
    st.close()
    for _ in range(2):
        assert deadman(env.settings, now, env.notifier) == ExitCode.OK
    assert len(env.notifier.sent) == (0 if source == "recent" else 1)
    if source != "recent":
        assert deadman(env.settings, now + timedelta(hours=7), env.notifier) == 0
        assert len(env.notifier.sent) == 2


def test_failed_delivery_can_retry(env):
    class Broken:
        def send(self, text):
            raise OSError("sensitive")

    now = env.clock.utcnow()
    st = env.state()
    st.start_run(INVOCATION, "sync", now.isoformat())
    a = Alert(kind="failure", text="synthetic")
    assert not AlertDispatcher(st, Broken(), dedup_hours=6).dispatch(a, now=now, run_id=INVOCATION)
    assert st.last_runs()[0]["alerted"] == 0
    for _ in range(2):
        assert AlertDispatcher(st, env.notifier, dedup_hours=6).dispatch(
            a, now=now, run_id=INVOCATION
        )
    assert st.last_runs()[0]["alerted"] == 1
    st.close()
    assert len(env.notifier.sent) == 1
    assert deadman(env.settings, now, Broken()) == ExitCode.PARTIAL
    assert deadman(env.settings, now, env.notifier) == ExitCode.OK


def test_hook_still_alerts_if_state_unavailable(env, tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.touch()
    settings = env.settings.replace(state_path=blocked / "state.db")
    assert notify_failure(settings, SYNC, INVOCATION, env.clock.utcnow(), env.notifier) == 0
    with run_lock(env.settings.lock_path), pytest.raises(LockBusy):
        notify_failure(env.settings, SYNC, INVOCATION, env.clock.utcnow(), env.notifier)
    assert len(env.notifier.sent) == 1


def test_monitor_cli(env, monkeypatch):
    monkeypatch.setattr("wispr_archiver.notify.make_notifier", lambda settings: env.notifier)
    runner = CliRunner(
        env={
            "WISPR_ARCHIVER_STATE_PATH": str(env.settings.state_path),
            "WISPR_ARCHIVER_TOKEN_PATH": str(env.settings.token_path),
        }
    )
    for args in [["deadman-check"], ["notify", "--unit", SYNC]]:
        assert runner.invoke(app, args).exit_code == 0
    assert runner.invoke(app, ["notify", "--unit", "untrusted text"]).exit_code != 0
    assert len(env.notifier.sent) == 2


@pytest.mark.anyio
async def test_blocked_reconcile_marks_alerted_even_when_deduplicated(tmp_path):
    server = FakeWisprServer([make_meeting(1)])
    env = make_env(tmp_path, server)
    assert (await env.run("backfill"))[0] == ExitCode.OK
    server.delete(make_meeting(1).id)
    for _ in range(2):
        env.clock.advance(minutes=1)
        assert (await env.run("reconcile"))[0] == ExitCode.PARTIAL
        st = State.open(env.settings.state_path)
        assert st.last_runs()[0]["alerted"] == 1
        st.close()
    assert len(env.notifier.sent) == 1
