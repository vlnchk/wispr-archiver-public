"""publish / verify-remote: rclone copy and check only, never anything that deletes."""

from __future__ import annotations

import io
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from tests.harness import RecordingNotifier
from wispr_archiver.archive.model import build_record
from wispr_archiver.archive.store import ArchiveStore
from wispr_archiver.config import Settings, load_settings
from wispr_archiver.errors import ConfigError, ExitCode, LockBusy, PublishError
from wispr_archiver.lock import run_lock
from wispr_archiver.log import setup_logging
from wispr_archiver.publish import (
    ALLOWED_SUBCOMMANDS,
    FORBIDDEN_TOKENS,
    ProcResult,
    Rclone,
    run_publish,
    run_verify_remote,
)
from wispr_archiver.state import State
from wispr_archiver.wispr.models import MeetingPayload

SECRET = "PRIVATE-FIXTURE-TITLE"


def settings(tmp: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "archive_dir": tmp / "archive",
        "state_path": tmp / "state" / "state.db",
        "token_path": tmp / "token" / "token.json",
        "rclone_remote": "gd:Wispr Archive",
        "rclone_config": tmp / "rclone.conf",
    }
    base.update(kw)
    nones = {k: None for k, v in base.items() if v is None}  # load_settings ignores None
    s = load_settings(None, env={}, **{k: v for k, v in base.items() if v is not None})
    return s.replace(**nones) if nones else s


def make_archive(root: Path, n: int = 2) -> ArchiveStore:
    store = ArchiveStore(root, ZoneInfo("Europe/Paris"), shrink_threshold=0.1)
    for i in range(n):
        meeting = MeetingPayload.model_validate(
            {
                "id": f"0000000{i}-aaaa-4000-8000-000000000000",
                "title": f"{SECRET} {i}",
                "summary": "s",
                "finalized": True,
                "has_transcript": True,
                "attendees": [],
                "todos": [],
                "start": "2001-02-03T09:00:00.000000Z",
                "end": "2001-02-03T09:30:00.000000Z",
                "modified_at": f"2001-02-03T09:4{i}:00.000000Z",
                "share_link": None,
            }
        )
        rec = build_record(
            meeting=meeting,
            participants=None,
            transcript=f"Speaker A:  {SECRET}\n",
            notes="",
            archived_at="t",
        )
        store.save(rec)
    return store


def stats_line(**kw: int) -> str:
    stats = {"bytes": 0, "checks": 0, "transfers": 0, "errors": 0, "deletes": 0, "renames": 0}
    stats.update(kw)
    return json.dumps({"level": "notice", "msg": "Transferred: ...", "stats": stats})


def file_line(level: str, msg: str) -> str:
    # rclone names objects in its log lines: these must never reach our log.
    return json.dumps({"level": level, "msg": msg, "object": f"meetings/2026/11/{SECRET}.md"})


@dataclass
class FakeRunner:
    script: Callable[[list[str]], tuple[int, str]] = lambda argv: (0, stats_line(transfers=1))
    calls: list[list[str]] = field(default_factory=list)
    envs: list[dict[str, str]] = field(default_factory=list)

    def __call__(self, argv: list[str], env: dict[str, str], timeout: float) -> ProcResult:
        self.calls.append(argv)
        self.envs.append(env)
        rc, stderr = self.script(argv)
        return ProcResult(returncode=rc, stderr=stderr)


def rclone(s: Settings, runner: FakeRunner, environ: dict[str, str] | None = None) -> Rclone:
    return Rclone(s, runner=runner, environ=environ if environ is not None else {"PATH": "/bin"})


def runs(s: Settings) -> list[dict[str, Any]]:
    state = State.open(s.state_path)
    try:
        return state.last_runs()
    finally:
        state.close()


def meta(s: Settings, key: str) -> str | None:
    state = State.open(s.state_path)
    try:
        return state.get_meta(key)
    finally:
        state.close()


# --- command lines ----------------------------------------------------------------------


def test_publish_runs_two_copies_with_safe_flags(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)
    runner = FakeRunner()
    assert run_publish(s, rclone=rclone(s, runner), notifier=RecordingNotifier()) == ExitCode.OK
    snaps, main = runner.calls
    root = str(s.archive_dir.resolve())
    assert snaps[:4] == ["rclone", "copy", f"{root}/_snapshots", "gd:Wispr Archive/_snapshots"]
    assert "--immutable" in snaps
    assert main[:4] == ["rclone", "copy", root, "gd:Wispr Archive"]
    assert "--immutable" not in main
    for argv in (snaps, main):
        assert "--checksum" in argv
        assert argv[argv.index("--config") + 1] == str(s.rclone_config)
        assert "--use-json-log" in argv
        assert "--dry-run" not in argv
    filters = [main[i + 1] for i, a in enumerate(main) if a == "--filter"]
    assert filters == ["- .*.tmp-*", "+ /meetings/**", "- **"]  # nothing but meetings/


@hsettings(max_examples=40)
@given(
    dry_run=st.booleans(),
    with_config=st.booleans(),
    folder=st.sampled_from(["Wispr Archive", "a/b", "sync", "delete", "purge me"]),
    remote=st.sampled_from(["gd", "wispr-gdrive", "sync", "move"]),
)
def test_no_deleting_subcommand_can_ever_be_built(
    tmp_path_factory: pytest.TempPathFactory,
    dry_run: bool,
    with_config: bool,
    folder: str,
    remote: str,
) -> None:
    tmp = tmp_path_factory.mktemp("p")
    s = settings(
        tmp,
        rclone_remote=f"{remote}:{folder}",
        rclone_config=(tmp / "c.conf") if with_config else None,
    )
    make_archive(s.archive_dir, n=1)
    runner = FakeRunner()
    run_publish(s, dry_run=dry_run, rclone=rclone(s, runner), notifier=RecordingNotifier())
    run_verify_remote(s, rclone=rclone(s, runner), notifier=RecordingNotifier())
    assert len(runner.calls) == 3
    for argv in runner.calls:
        assert argv[1] in ALLOWED_SUBCOMMANDS
        assert not FORBIDDEN_TOKENS.intersection(argv), argv
        assert not any(a.startswith(("--delete", "--max-delete", "--backup-dir")) for a in argv)
        assert ("--dry-run" in argv) == (dry_run and argv[1] == "copy")


@pytest.mark.parametrize("sub", ["sync", "bisync", "move", "delete", "purge", "dedupe", "rmdirs"])
def test_rclone_refuses_other_subcommands(tmp_path: Path, sub: str) -> None:
    r = rclone(settings(tmp_path), FakeRunner())
    with pytest.raises(ValueError, match="not allowed"):
        r.argv(sub, "a", "b")


@pytest.mark.parametrize("remote", ["gd", "gd:", "gd:/", "gd: ", ":local:", "-gd:x", ""])
def test_remote_must_name_a_folder(tmp_path: Path, remote: str) -> None:
    with pytest.raises(ConfigError):
        settings(tmp_path, rclone_remote=remote)


@pytest.mark.parametrize("remote", ["wispr-gdrive:Wispr Archive", ":local:/srv/x", "gd:a/b"])
def test_valid_remotes(tmp_path: Path, remote: str) -> None:
    assert settings(tmp_path, rclone_remote=remote).rclone_remote == remote


def test_rclone_env_is_minimal(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir, n=1)
    runner = FakeRunner()
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "HTTPS_PROXY": "http://proxy:3128",
        "RCLONE_CONFIG_PASS": "pw",
        "WISPR_ARCHIVER_TELEGRAM_BOT_TOKEN": "123:abc",
        "AWS_SECRET_ACCESS_KEY": "nope",
    }
    run_publish(s, rclone=rclone(s, runner, environ), notifier=RecordingNotifier())
    env = runner.envs[0]
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/x"
    assert env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["RCLONE_CONFIG_PASS"] == "pw"  # an encrypted rclone.conf needs it
    assert "WISPR_ARCHIVER_TELEGRAM_BOT_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


# --- outcomes ---------------------------------------------------------------------------


def test_success_records_run_and_timestamp(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)
    notifier = RecordingNotifier()
    runner = FakeRunner(lambda argv: (0, stats_line(transfers=3, bytes=1000)))
    assert run_publish(s, rclone=rclone(s, runner), notifier=notifier) == ExitCode.OK
    (run,) = runs(s)
    assert (run["command"], run["exit_code"], run["completed"]) == ("publish", 0, 1)
    assert json.loads(run["summary"])["transfers"] == 6
    assert meta(s, "last_published_at") is not None
    assert notifier.sent == []


def test_immutable_clash_fails_but_main_copy_still_runs(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)

    def script(argv: list[str]) -> tuple[int, str]:
        if "--immutable" in argv:  # a remote snapshot differs: rclone refuses and exits 6
            return 6, "\n".join(
                [file_line("error", "immutable file modified"), stats_line(errors=1)]
            )
        return 0, stats_line(transfers=2)

    notifier = RecordingNotifier()
    runner = FakeRunner(script)
    assert run_publish(s, rclone=rclone(s, runner), notifier=notifier) == ExitCode.PARTIAL
    assert len(runner.calls) == 2
    assert meta(s, "last_published_at") is None
    (text,) = notifier.sent
    assert "snapshots" in text
    assert "RUNBOOK" in text
    assert SECRET not in text
    run_publish(s, rclone=rclone(s, runner), notifier=notifier)
    assert len(notifier.sent) == 1  # same problem within 6 h: not repeated


def test_reported_deletes_are_an_error(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)
    runner = FakeRunner(lambda argv: (0, stats_line(deletes=1)))
    notifier = RecordingNotifier()
    assert run_publish(s, rclone=rclone(s, runner), notifier=notifier) == ExitCode.PARTIAL
    assert "unexpected_deletes" in notifier.sent[0]


@pytest.mark.parametrize("kind", ["rclone_missing", "rclone_timeout"])
def test_runner_failures_become_partial(tmp_path: Path, kind: str) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)

    def script(argv: list[str]) -> tuple[int, str]:
        raise PublishError("boom", kind=kind)

    notifier = RecordingNotifier()
    assert run_publish(s, rclone=rclone(s, FakeRunner(script)), notifier=notifier) == 1
    assert kind in notifier.sent[0]


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (
            json.dumps(
                {
                    "level": "critical",
                    "msg": 'Failed to create file system for "gd:x": '
                    'didn\'t find section in config file ("gd")',
                }
            ),
            "remote_not_configured",
        ),
        (
            json.dumps(
                {
                    "level": "error",
                    "msg": 'couldn\'t fetch token: oauth2: "invalid_grant" "Token has been '
                    'expired or revoked."',
                }
            ),
            "drive_auth",
        ),
        (
            json.dumps(
                {
                    "level": "error",
                    "msg": "googleapi: Error 403: User rate limit exceeded., userRateLimitExceeded",
                }
            ),
            "drive_quota",
        ),
    ],
)
def test_known_rclone_failures_are_named(tmp_path: Path, line: str, reason: str) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)
    notifier = RecordingNotifier()
    runner = FakeRunner(lambda argv: (1, line))
    assert run_publish(s, rclone=rclone(s, runner), notifier=notifier) == ExitCode.PARTIAL
    assert f"snapshots:{reason}" in notifier.sent[0]
    assert f"main:{reason}" in notifier.sent[0]
    assert "rclone_exit_1" not in notifier.sent[0]
    report: dict[str, Any] = {}
    run_verify_remote(s, rclone=rclone(s, runner), report=report)
    assert report["ok"] is False
    (run,) = [r for r in runs(s) if r["command"] == "verify-remote"]
    assert reason in json.loads(run["summary"])["problems"]


def test_unknown_rclone_failure_keeps_the_exit_code(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)
    notifier = RecordingNotifier()
    runner = FakeRunner(lambda argv: (7, file_line("critical", "something new")))
    run_publish(s, rclone=rclone(s, runner), notifier=notifier)
    assert "main:rclone_exit_7" in notifier.sent[0]


def test_missing_archive_is_refused_without_calling_rclone(tmp_path: Path) -> None:
    s = settings(tmp_path)  # archive_dir does not exist (e.g. a disk not mounted)
    runner = FakeRunner()
    notifier = RecordingNotifier()
    assert run_publish(s, rclone=rclone(s, runner), notifier=notifier) == ExitCode.PARTIAL
    assert runner.calls == []
    assert "archive_missing" in notifier.sent[0]
    assert run_verify_remote(s, rclone=rclone(s, runner), notifier=notifier) == ExitCode.PARTIAL
    assert runner.calls == []


def test_rclone_log_lines_never_reach_our_log(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)
    buf = io.StringIO()
    setup_logging("DEBUG", stream=buf)
    noisy = "\n".join(
        [
            file_line("error", f"failed to copy {SECRET}"),
            f"plain text line {SECRET}",
            stats_line(errors=1),
        ]
    )
    run_publish(s, rclone=rclone(s, FakeRunner(lambda a: (1, noisy))), notifier=RecordingNotifier())
    out = buf.getvalue()
    assert out
    assert SECRET not in out
    for line in out.splitlines():
        assert "dropped_fields" not in json.loads(line), line


def test_dry_run_passes_flag_and_keeps_state(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)
    runner = FakeRunner()
    assert run_publish(s, dry_run=True, rclone=rclone(s, runner)) == ExitCode.OK
    assert all("--dry-run" in argv for argv in runner.calls)
    assert not s.state_path.exists()


def test_publish_holds_the_run_lock(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)
    with run_lock(s.lock_path), pytest.raises(LockBusy):
        run_publish(s, rclone=rclone(s, FakeRunner()), notifier=RecordingNotifier())


# --- verify-remote ----------------------------------------------------------------------


def check_script(**lines: int) -> Callable[[list[str]], tuple[int, str]]:
    def script(argv: list[str]) -> tuple[int, str]:
        flags = {"missing": "--missing-on-dst", "differ": "--differ", "error": "--error"}
        flags["match"] = "--match"
        for key, flag in flags.items():
            n = lines.get(key, 0)
            Path(argv[argv.index(flag) + 1]).write_text(
                "".join(f"{SECRET}-{i}\n" for i in range(n))
            )
        bad = any(lines.get(k, 0) for k in ("missing", "differ", "error"))
        return (1 if bad else 0), file_line("notice", "0 differences found")

    return script


def test_verify_remote_ok(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)  # 2 meetings: 2 json + 2 md + 2 snapshots
    runner = FakeRunner(check_script(match=6))
    report: dict[str, Any] = {}
    assert run_verify_remote(s, rclone=rclone(s, runner), report=report) == ExitCode.OK
    (argv,) = runner.calls
    root = str(s.archive_dir.resolve())
    assert argv[:4] == ["rclone", "check", root, "gd:Wispr Archive"]
    assert "--one-way" in argv
    filters = [argv[i + 1] for i, a in enumerate(argv) if a == "--filter"]
    assert filters == ["- .*.tmp-*", "+ /meetings/**", "+ /_snapshots/**", "- **"]
    assert report == {
        "local_files": 6,
        "matched": 6,
        "missing": 0,
        "differ": 0,
        "errors": 0,
        "rclone_exit": 0,
        "ok": True,
    }


@pytest.mark.parametrize(
    ("lines", "problem"),
    [({"match": 5, "missing": 1}, "missing"), ({"match": 5, "differ": 1}, "differ")],
)
def test_verify_remote_reports_problems(
    tmp_path: Path, lines: dict[str, int], problem: str
) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)
    report: dict[str, Any] = {}
    notifier = RecordingNotifier()
    code = run_verify_remote(
        s, rclone=rclone(s, FakeRunner(check_script(**lines))), notifier=notifier, report=report
    )
    assert code == ExitCode.PARTIAL
    assert report[problem] == 1
    assert report["ok"] is False
    assert SECRET not in json.dumps(report)
    assert problem in notifier.sent[0]


def test_verify_remote_catches_unmatched_files(tmp_path: Path) -> None:
    s = settings(tmp_path)
    make_archive(s.archive_dir)
    report: dict[str, Any] = {}
    code = run_verify_remote(s, rclone=rclone(s, FakeRunner(check_script(match=4))), report=report)
    assert code == ExitCode.PARTIAL  # rclone said fine, but 2 local files were not compared
    assert report["matched"] == 4


# --- real rclone, local folders only (no Drive, no network) -----------------------------

RCLONE = shutil.which("rclone")
needs_rclone = pytest.mark.skipif(RCLONE is None, reason="rclone not installed")


def real(tmp_path: Path) -> Settings:
    (tmp_path / "rclone.conf").write_text("")  # never read the user's own rclone.conf
    return settings(tmp_path, rclone_remote=f":local:{tmp_path / 'remote'}")


@needs_rclone
def test_real_rclone_roundtrip(tmp_path: Path) -> None:
    s = real(tmp_path)
    store = make_archive(s.archive_dir)
    (s.archive_dir / "meetings" / ".x.json.tmp-1-abcd").write_text("partial write")
    (s.archive_dir / "stray.txt").write_text("not part of the archive")
    remote = tmp_path / "remote"
    notifier = RecordingNotifier()

    assert run_publish(s, notifier=notifier) == ExitCode.OK
    local = sorted(p.relative_to(s.archive_dir).as_posix() for p in store.root.rglob("*.json"))
    local += sorted(p.relative_to(s.archive_dir).as_posix() for p in store.root.rglob("*.md"))
    copied = sorted(p.relative_to(remote).as_posix() for p in remote.rglob("*") if p.is_file())
    assert copied == sorted(local)  # no temp file, no stray file
    assert json.loads(runs(s)[0]["summary"])["transfers"] == 6

    (remote / "meetings" / "kept-forever.txt").write_text("only on the remote")
    assert run_publish(s, notifier=notifier) == ExitCode.OK
    assert json.loads(runs(s)[0]["summary"])["transfers"] == 0  # idempotent
    assert (remote / "meetings" / "kept-forever.txt").exists()  # copy never deletes

    report: dict[str, Any] = {}
    assert run_verify_remote(s, notifier=notifier, report=report) == ExitCode.OK
    assert report["matched"] == report["local_files"] == 6
    assert notifier.sent == []


@needs_rclone
def test_real_rclone_never_changes_a_remote_snapshot(tmp_path: Path) -> None:
    s = real(tmp_path)
    make_archive(s.archive_dir, n=1)
    assert run_publish(s, notifier=RecordingNotifier()) == ExitCode.OK
    (snap,) = (tmp_path / "remote" / "_snapshots").rglob("*.json")
    snap.write_text('{"tampered": true}')
    main = next((tmp_path / "remote" / "meetings").rglob("*.md"))
    main.unlink()

    notifier = RecordingNotifier()
    assert run_publish(s, notifier=notifier) == ExitCode.PARTIAL
    assert snap.read_text() == '{"tampered": true}'  # --immutable: left alone, reported
    assert main.exists()  # the main copy still ran and restored the missing file
    assert "snapshots" in notifier.sent[0]

    report: dict[str, Any] = {}
    assert run_verify_remote(s, report=report) == ExitCode.PARTIAL
    assert (report["differ"], report["missing"]) == (1, 0)


@needs_rclone
def test_real_rclone_names_an_unconfigured_remote(tmp_path: Path) -> None:
    (tmp_path / "rclone.conf").write_text("")
    s = settings(tmp_path, rclone_remote="wispr-gdrive:Wispr Archive")  # not in rclone.conf
    make_archive(s.archive_dir, n=1)
    notifier = RecordingNotifier()
    assert run_publish(s, notifier=notifier) == ExitCode.PARTIAL
    assert "snapshots:remote_not_configured" in notifier.sent[0]
    assert "main:remote_not_configured" in notifier.sent[0]
