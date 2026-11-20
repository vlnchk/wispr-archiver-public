"""Manage a per-user launchd job for an existing, configured Mac archive."""

import argparse
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from wispr_archiver.archive.store import write_new_only
from wispr_archiver.config import Settings, load_settings
from wispr_archiver.errors import ArchiverError, LockBusy
from wispr_archiver.lock import run_lock
from wispr_archiver.state import State

LABEL = "local.wispr-archiver"
CONFIG = Path.home() / ".config/wispr-archiver/mac.toml"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
SHORTCUT = Path.home() / ".local/bin/wispr-mac"
ENTRY = Path(sys.executable).with_name("wispr-mac")
DOMAIN = f"gui/{os.getuid()}"
SERVICE = f"{DOMAIN}/{LABEL}"


def settings() -> Settings:
    return load_settings(CONFIG)


def cycle_lock() -> Path:
    return CONFIG.with_name("mac-cycle.lock")


def worker(command: str) -> int:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "wispr_archiver.cli", command, "--config", str(CONFIG)],
        check=False,
    ).returncode


def cycle() -> int:
    with run_lock(cycle_lock(), wait=True):
        s = settings()
        # Open the established state read-only. A missing disk must not start a new archive.
        if not s.archive_dir.is_dir():
            raise ValueError(f"Archive directory missing: {s.archive_dir}")
        with closing(sqlite3.connect(s.state_path.as_uri() + "?mode=ro", uri=True)) as db:
            recent = db.execute(
                "SELECT 1 FROM runs WHERE command='reconcile' AND completed=1 AND exit_code=0 "
                "AND julianday(finished_at) >= julianday('now', '-7 days') LIMIT 1"
            ).fetchone()
        ingest = worker("sync" if recent else "reconcile")
        publish = worker("publish")  # A partial run can already have committed new versions.
        return ingest or publish


def ctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/bin/launchctl", *args], check=False, capture_output=True, text=True
    )


def job() -> str | None:
    result = ctl("print", SERVICE)
    if result.returncode == 113:  # launchctl: service not found in this domain
        return None
    result.check_returncode()
    return result.stdout


def plist_bytes() -> bytes:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [sys.executable, "-m", "wispr_archiver.mac", "_cycle"],
            "RunAtLoad": True,
            "StartCalendarInterval": [{"Minute": 0}, {"Minute": 30}],
            "ProcessType": "Background",
            "Umask": 0o077,
        }
    )


def check_plist() -> None:
    if PLIST.is_symlink() or (PLIST.exists() and PLIST.read_bytes() != plist_bytes()):
        raise ValueError(f"Unexpected plist at {PLIST}. Inspect before changing it.")


def start() -> None:
    with run_lock(cycle_lock()):
        check_plist()
        s = settings()
        if not s.archive_dir.is_absolute() or not s.state_path.is_absolute():
            raise ValueError(f"Use absolute archive_dir and state_path in {CONFIG}")
        if not s.state_path.is_file() or not s.archive_dir.is_dir():
            raise ValueError(f"Check the existing archive and state paths in {CONFIG}")
        if job() is not None:
            if not PLIST.exists():
                raise ValueError(f"Service {SERVICE} already loaded without our plist")
            return
        created = write_new_only(PLIST, plist_bytes())
        check_plist()
        try:
            ctl("bootstrap", DOMAIN, str(PLIST)).check_returncode()
        except subprocess.CalledProcessError:
            if job() is None:
                if created:
                    PLIST.unlink(missing_ok=True)
                raise


def stop(*, uninstall: bool) -> None:
    with run_lock(cycle_lock()):  # Never interrupt a rotating token or a publication.
        check_plist()
        if job() is not None:
            if not PLIST.exists():
                raise ValueError(f"Service {SERVICE} already loaded without our plist")
            ctl("bootout", SERVICE).check_returncode()
        PLIST.unlink(missing_ok=True)
        if uninstall and SHORTCUT.is_symlink() and SHORTCUT.resolve() == ENTRY.resolve():
            SHORTCUT.unlink()


def status() -> int:
    info = job()
    print(f"Schedule: {'enabled' if info is not None else 'off'}", flush=True)
    if info is not None:
        # launchctl print includes the environment. Only expose these two service fields.
        for line in info.splitlines():
            if line.startswith(("\tstate = ", "\tlast exit code = ")):
                print(line.strip(), flush=True)
    s = settings()
    if not s.state_path.is_file():
        raise ValueError(f"Missing state: {s.state_path}")
    if not s.archive_dir.is_dir():
        raise ValueError(f"Missing archive: {s.archive_dir}")
    with closing(State.open(s.state_path, dry_run=True)) as db:
        print("Last completed sync/reconcile:", db.get_meta("last_completed_at") or "—")
        print("Last published:", db.get_meta("last_published_at") or "—")
        for run in db.last_runs(5):
            problems = json.loads(run["summary"] or "{}").get("problems", [])
            result = f"exit {run['exit_code']}" if run["finished_at"] else "unfinished"
            print(run["started_at"], run["command"], result, *problems)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the macOS Wispr archive schedule.")
    parser.add_argument("command", choices=["start", "status", "stop", "uninstall"])
    if sys.platform != "darwin":
        parser.exit(1, "This command requires macOS.\n")
    os.umask(os.umask(0o077) | 0o077)
    try:
        if sys.argv[1:] == ["_cycle"]:
            return cycle()
        cmd = parser.parse_args().command
        if cmd == "status":
            return status()
        if cmd == "start":
            start()
            print("Schedule enabled. Check wispr-mac status.")
        else:
            stop(uninstall=cmd == "uninstall")
            print("Schedule removed. Archive and credentials preserved.")
        return 0
    except LockBusy:
        print("An archive job is running. Retry after it finishes.", file=sys.stderr)
        return 1
    except (ArchiverError, OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as e:
        print(f"wispr-mac: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
