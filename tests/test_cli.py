from __future__ import annotations

from typer.testing import CliRunner

from wispr_archiver.cli import app

COMMANDS = [
    "login",
    "probe",
    "refresh-check",
    "backfill",
    "sync",
    "reconcile",
    "verify",
    "status",
    "rebuild-state",
    "publish",
    "verify-remote",
    "notify",
    "deadman-check",
]


def test_help_lists_all_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in COMMANDS:
        assert cmd in result.output


def test_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == "0.1.0"


def test_cli_tightens_umask_never_loosens() -> None:
    import os

    os.umask(0o002)
    CliRunner().invoke(app, ["verify", "--dest", "/nonexistent-archive"])
    assert os.umask(0o077) == 0o027  # group may read, others nothing (state DB, WAL too)
    CliRunner().invoke(app, ["verify", "--dest", "/nonexistent-archive"])
    assert os.umask(0o022) == 0o077  # a stricter umask is kept


def test_offline_commands(tmp_path) -> None:
    import json

    import anyio

    from tests.fake_wispr import FakeWisprServer, make_meeting
    from tests.harness import make_env

    env = make_env(tmp_path, FakeWisprServer([make_meeting(1), make_meeting(2)]))
    anyio.run(env.run, "backfill")
    common = ["--dest", str(env.archive)]
    runner = CliRunner()
    env_vars = {
        "WISPR_ARCHIVER_STATE_PATH": str(env.settings.state_path),
        "WISPR_ARCHIVER_TIMEZONE": env.settings.timezone,
    }

    r = runner.invoke(app, ["verify", *common], env=env_vars)
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    report = json.loads("\n".join(lines[lines.index("{") :]))  # pretty report after JSON logs
    assert report["checked"] == 2
    assert report["problems"] == []

    r = runner.invoke(app, ["status", *common], env=env_vars)
    assert r.exit_code == 0, r.output
    assert '"meetings_by_status"' in r.output

    r = runner.invoke(app, ["rebuild-state", *common], env=env_vars)
    assert r.exit_code == 0, r.output
    assert '"meetings": 2' in r.output


def test_config_error_exit_code(tmp_path) -> None:
    r = CliRunner().invoke(app, ["verify"], env={"WISPR_ARCHIVER_CHUNK_SIZE": "99999"})
    assert r.exit_code == 1


def test_publish_and_verify_remote_via_cli_with_real_rclone(tmp_path) -> None:
    import json
    import shutil

    import pytest

    if shutil.which("rclone") is None:
        pytest.skip("rclone not installed")
    from tests.test_publish import make_archive

    make_archive(tmp_path / "archive", n=1)
    (tmp_path / "rclone.conf").write_text("")
    env = {
        "WISPR_ARCHIVER_TOKEN_PATH": str(tmp_path / "token" / "token.json"),  # lock lives here
        "WISPR_ARCHIVER_RCLONE_CONFIG": str(tmp_path / "rclone.conf"),
    }
    common = ["--dest", str(tmp_path / "archive"), "--state", str(tmp_path / "state.db")]
    remote = ["--remote", f":local:{tmp_path / 'remote'}"]
    runner = CliRunner(env=env)

    dry = runner.invoke(app, ["publish", *common, *remote, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert not (tmp_path / "remote").exists()

    result = runner.invoke(app, ["publish", *common, *remote])
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "remote").rglob("*.json"))) == 2  # main + snapshot

    check = runner.invoke(app, ["verify-remote", *common, *remote])
    assert check.exit_code == 0, check.output
    report = json.loads(check.output[check.output.index("{\n") :])
    assert (report["ok"], report["matched"], report["local_files"]) == (True, 3, 3)

    status = runner.invoke(app, ["status", *common])
    assert json.loads(status.output)["last_published_at"] is not None
