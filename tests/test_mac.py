"""Offline coverage for the per-user macOS scheduler."""

import plistlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wispr_archiver import mac
from wispr_archiver.config import Settings
from wispr_archiver.errors import LockBusy
from wispr_archiver.lock import run_lock
from wispr_archiver.state import State


@pytest.fixture
def settings(tmp_path, monkeypatch):
    cfg = tmp_path / "mac.toml"
    cfg.touch()
    monkeypatch.setattr(mac, "CONFIG", cfg)
    monkeypatch.setattr(mac, "PLIST", tmp_path / "agents" / "test.plist")
    store = tmp_path / "archive"
    store.mkdir()
    s = Settings(archive_dir=store, state_path=tmp_path / "state.db")
    State.open(s.state_path).close()
    monkeypatch.setattr(mac, "settings", lambda: s)
    return s


def test_plist_and_uninstall(settings, monkeypatch, tmp_path):
    calls = []
    active = False

    def ctl(*args):
        nonlocal active
        calls.append(args[0])
        if args[0] == "print":
            return subprocess.CompletedProcess(
                args, 0 if active else 113, "\tstate = waiting\n", ""
            )
        active = args[0] == "bootstrap"
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(mac, "ctl", ctl)
    token = tmp_path / "token.json"
    token.touch()
    data = settings.archive_dir / "keep"
    data.touch()
    cmd = tmp_path / "bin" / "wispr-mac"
    cmd.parent.mkdir()
    cmd.symlink_to(mac.ENTRY)
    monkeypatch.setattr(mac, "SHORTCUT", cmd)
    mac.start()
    mac.start()
    p = plistlib.loads(mac.PLIST.read_bytes())
    assert p["RunAtLoad"]
    assert p["StartCalendarInterval"] == [{"Minute": 0}, {"Minute": 30}]
    assert p["ProgramArguments"][-1] == "_cycle"
    assert Path(p["ProgramArguments"][0]).is_absolute()
    assert "EnvironmentVariables" not in p
    assert calls.count("bootstrap") == 1
    with run_lock(mac.cycle_lock()), pytest.raises(LockBusy):
        mac.stop(uninstall=True)
    assert cmd.is_symlink()
    assert mac.PLIST.is_file()
    mac.stop(uninstall=True)
    mac.stop(uninstall=True)
    assert not mac.PLIST.exists()
    assert not cmd.is_symlink()
    assert mac.CONFIG.exists()
    assert token.exists()
    assert data.exists()
    assert settings.state_path.exists()


def test_cycle_publishes_partial_and_reconciles_weekly(settings, monkeypatch):
    calls = []

    def worker(cmd):
        calls.append(cmd)
        return 3 if cmd == "reconcile" else 0

    monkeypatch.setattr(mac, "worker", worker)
    assert mac.cycle() == 3
    assert calls == ["reconcile", "publish"]
    db = State.open(settings.state_path)
    now = datetime.now(UTC)
    try:
        for days, expected in [(8, "reconcile"), (1, "sync")]:
            at = (now - timedelta(days=days)).isoformat()
            db.start_run(str(days), "reconcile", at)
            db.finish_run(str(days), at=at, exit_code=0, completed=True, summary={})
            calls.clear()
            mac.cycle()
            assert calls == [expected, "publish"]
    finally:
        db.close()


def test_foreign_plist_and_shortcut_are_preserved(settings, monkeypatch, tmp_path):
    mac.PLIST.parent.mkdir()
    mac.PLIST.write_bytes(b"belongs to another job")
    with pytest.raises(ValueError, match="plist"):
        mac.start()
    with pytest.raises(ValueError, match="plist"):
        mac.stop(uninstall=True)
    assert mac.PLIST.read_bytes() == b"belongs to another job"
    mac.PLIST.unlink()
    other = tmp_path / "unrelated"
    other.touch()
    shortcut = tmp_path / "shortcut"
    shortcut.symlink_to(other)
    monkeypatch.setattr(mac, "SHORTCUT", shortcut)
    monkeypatch.setattr(mac, "ctl", lambda *a: subprocess.CompletedProcess(a, 113, "", ""))
    mac.stop(uninstall=True)
    assert shortcut.is_symlink()


def test_start_rolls_back_registration_error(settings, monkeypatch):
    def denied(*args):
        return subprocess.CompletedProcess(args, 113 if args[0] == "print" else 5, "", "")

    monkeypatch.setattr(mac, "ctl", denied)
    with pytest.raises(subprocess.CalledProcessError):
        mac.start()
    assert not mac.PLIST.exists()


def test_status_reports_outcome_without_launch_environment(settings, monkeypatch, capsys):
    monkeypatch.setattr(mac, "job", lambda: "\tstate = waiting\n\tenvironment = private-value")
    assert mac.status() == 0
    out = capsys.readouterr().out
    assert "waiting" in out
    assert "Last published" in out
    assert "private-value" not in out
