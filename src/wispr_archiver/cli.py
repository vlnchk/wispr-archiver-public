"""Command line interface (typer). Every command exits with an ExitCode."""

# No `from __future__ import annotations`: typer inspects annotations at runtime.

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import anyio
import typer

from wispr_archiver import __version__
from wispr_archiver.archive.store import ArchiveStore, verify_archive
from wispr_archiver.config import Settings, load_settings
from wispr_archiver.errors import ArchiverError, ExitCode
from wispr_archiver.lock import run_lock
from wispr_archiver.log import error, event, setup_logging
from wispr_archiver.maintenance import rebuild_state as do_rebuild_state
from wispr_archiver.maintenance import status_report
from wispr_archiver.state import State
from wispr_archiver.wispr.client import iso_z

# Imported lazily inside commands that need the network stack: runner.

app = typer.Typer(
    name="wispr-archiver",
    help="Append-only archive of Wispr Flow Notetaker meetings (official Remote MCP only).",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

ConfigOpt = Annotated[
    Path | None,
    typer.Option("--config", help="TOML config (default: /etc/wispr-archiver/config.toml)."),
]
DestOpt = Annotated[
    Path | None, typer.Option("--dest", help="Archive directory (overrides config).")
]
StateOpt = Annotated[
    Path | None, typer.Option("--state", help="SQLite state file (overrides config).")
]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Fetch and compute, write nothing.")]


# Everything this process creates (state DB and its WAL/SHM, probe output, archive) is
# private: group may read, others get nothing. A stricter umask is kept.
PRIVATE_UMASK = 0o027


def _tighten_umask() -> None:
    os.umask(os.umask(PRIVATE_UMASK) | PRIVATE_UMASK)


class _Ctx:
    config_path: Path | None = None
    log_level: str = "INFO"


_ctx = _Ctx()


def _print_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit(0)


@app.callback()
def _main(
    log_level: Annotated[str, typer.Option("--log-level", help="DEBUG, INFO, WARNING.")] = "INFO",
    version: Annotated[
        bool,
        typer.Option(
            "--version", help="Print version and exit.", is_eager=True, callback=_print_version
        ),
    ] = False,
) -> None:
    _tighten_umask()
    _ctx.log_level = log_level
    setup_logging(log_level)


def _settings(config: Path | None, **overrides: Any) -> Settings:
    return load_settings(config, **overrides)


def _store(s: Settings, *, dry_run: bool = False) -> ArchiveStore:
    return ArchiveStore(s.archive_dir, s.tz, shrink_threshold=s.shrink_threshold, dry_run=dry_run)


def _print_json(data: Any) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _finish(code: ExitCode) -> NoReturn:
    raise typer.Exit(int(code))


def _fail(command: str, exc: ArchiverError) -> NoReturn:
    error(
        "command_failed",
        command=command,
        error_class=type(exc).__name__,
        error_kind=exc.kind,
        error=str(exc),
        exit_code=int(exc.exit_code),
    )
    _finish(exc.exit_code)


def _engine(
    command: str, config: Path | None, dest: Path | None, state: Path | None, dry_run: bool
) -> NoReturn:
    from wispr_archiver.runner import run_engine

    try:
        s = _settings(config, archive_dir=dest, state_path=state)
        code = anyio.run(lambda: run_engine(s, command, dry_run=dry_run))  # type: ignore[arg-type]
    except ArchiverError as exc:
        _fail(command, exc)
    _finish(code)


@app.command()
def login(
    config: ConfigOpt = None,
    no_browser: Annotated[
        bool,
        typer.Option(
            "--no-browser", help="Print the URL instead of opening a browser (for ssh -L)."
        ),
    ] = False,
    port: Annotated[int | None, typer.Option("--port", help="Loopback redirect port.")] = None,
) -> None:
    """Interactive OAuth login to Wispr (human only)."""
    from wispr_archiver.runner import run_login

    try:
        s = _settings(config, login_port=port)
        summary = anyio.run(lambda: run_login(s, open_browser=not no_browser))
    except ArchiverError as exc:
        _fail("login", exc)
    _print_json(summary)
    _finish(ExitCode.OK)


@app.command()
def probe(
    config: ConfigOpt = None,
    out: Annotated[
        Path, typer.Option("--out", help="Where to write the structure-only report.")
    ] = Path("wispr-probe.json"),
) -> None:
    """Read-only probe of the Wispr MCP response format (structure only, no content)."""
    from wispr_archiver.runner import run_probe

    try:
        s = _settings(config)
        report = anyio.run(lambda: run_probe(s, out))
    except ArchiverError as exc:
        _fail("probe", exc)
    typer.echo(f"probe report written to {out} ({len(report.get('errors', []))} section errors)")
    _finish(ExitCode.OK)


@app.command("refresh-check")
def refresh_check(config: ConfigOpt = None) -> None:
    """Force token refresh twice and verify the rotated token works."""
    from wispr_archiver.runner import run_refresh_check

    try:
        s = _settings(config)
        report = anyio.run(lambda: run_refresh_check(s))
    except ArchiverError as exc:
        _fail("refresh-check", exc)
    _print_json(report)
    _finish(ExitCode.OK)


@app.command()
def backfill(
    config: ConfigOpt = None,
    dest: DestOpt = None,
    state: StateOpt = None,
    dry_run: DryRunOpt = False,
) -> None:
    """Archive the whole history."""
    _engine("backfill", config, dest, state, dry_run)


@app.command()
def sync(
    config: ConfigOpt = None,
    dest: DestOpt = None,
    state: StateOpt = None,
    dry_run: DryRunOpt = False,
) -> None:
    """Incremental sync (run by the 30-minute timer)."""
    _engine("sync", config, dest, state, dry_run)


@app.command()
def reconcile(
    config: ConfigOpt = None,
    dest: DestOpt = None,
    state: StateOpt = None,
    dry_run: DryRunOpt = False,
) -> None:
    """Full id listing: fetch missed meetings, mark deleted_upstream_at (never deletes)."""
    _engine("reconcile", config, dest, state, dry_run)


@app.command()
def verify(config: ConfigOpt = None, dest: DestOpt = None) -> None:
    """Recompute hashes and check lengths of the local archive."""
    try:
        s = _settings(config, archive_dir=dest)
    except ArchiverError as exc:
        _fail("verify", exc)
    report = verify_archive(_store(s))
    problems = report.problems or []
    event(
        "verify_finished",
        command="verify",
        checked=report.checked,
        snapshots=report.snapshots,
        problems=len(problems),
    )
    _print_json(
        {
            "checked": report.checked,
            "snapshots": report.snapshots,
            "problems": [{"meeting_id": m, "problem": p} for m, p in problems],
        }
    )
    _finish(ExitCode.OK if report.ok else ExitCode.PARTIAL)


@app.command()
def status(config: ConfigOpt = None, dest: DestOpt = None, state: StateOpt = None) -> None:
    """Show archive and run status."""
    try:
        s = _settings(config, archive_dir=dest, state_path=state)
    except ArchiverError as exc:
        _fail("status", exc)
    st = State.open(s.state_path, dry_run=True)  # read-only view
    try:
        _print_json(status_report(st, _store(s), now=datetime.now(UTC)))
    finally:
        st.close()
    _finish(ExitCode.OK)


@app.command("rebuild-state")
def rebuild_state(config: ConfigOpt = None, dest: DestOpt = None, state: StateOpt = None) -> None:
    """Rebuild the meetings table from the JSON archive."""
    try:
        s = _settings(config, archive_dir=dest, state_path=state)
        with run_lock(s.lock_path):
            count, backup = do_rebuild_state(_store(s), s.state_path, now=iso_z(datetime.now(UTC)))
    except ArchiverError as exc:
        _fail("rebuild-state", exc)
    event("state_rebuilt", command="rebuild-state", count=count)
    _print_json({"meetings": count, "previous_state_backup": str(backup) if backup else None})
    _finish(ExitCode.OK)


RemoteOpt = Annotated[
    str | None, typer.Option("--remote", help="rclone remote:folder (overrides config).")
]


@app.command()
def publish(
    config: ConfigOpt = None,
    dest: DestOpt = None,
    state: StateOpt = None,
    remote: RemoteOpt = None,
    dry_run: DryRunOpt = False,
) -> None:
    """Replicate the archive to Google Drive with `rclone copy` (never sync or delete)."""
    from wispr_archiver.publish import run_publish

    try:
        s = _settings(config, archive_dir=dest, state_path=state, rclone_remote=remote)
        code = run_publish(s, dry_run=dry_run)
    except ArchiverError as exc:
        _fail("publish", exc)
    _finish(code)


@app.command("verify-remote")
def verify_remote(
    config: ConfigOpt = None,
    dest: DestOpt = None,
    state: StateOpt = None,
    remote: RemoteOpt = None,
) -> None:
    """Check with `rclone check --one-way` that every archive file is on the remote, same hash."""
    from wispr_archiver.publish import run_verify_remote

    report: dict[str, Any] = {}
    try:
        s = _settings(config, archive_dir=dest, state_path=state, rclone_remote=remote)
        code = run_verify_remote(s, report=report)
    except ArchiverError as exc:
        _fail("verify-remote", exc)
    _print_json(report)
    _finish(code)


@app.command()
def notify(unit: Annotated[str, typer.Option("--unit")], config: ConfigOpt = None) -> None:
    """Send an alert for a failed systemd unit (OnFailure hook)."""
    from wispr_archiver.notify import notify_failure

    try:
        code = notify_failure(
            _settings(config), unit, os.environ.get("MONITOR_INVOCATION_ID", ""), datetime.now(UTC)
        )
    except ArchiverError as exc:
        _fail("notify", exc)
    _finish(code)


@app.command("deadman-check")
def deadman_check(config: ConfigOpt = None, state: StateOpt = None) -> None:
    """Alert when archive runs have stopped reaching completion."""
    from wispr_archiver.notify import deadman

    try:
        code = deadman(_settings(config, state_path=state), datetime.now(UTC))
    except ArchiverError as exc:
        _fail("deadman-check", exc)
    _finish(code)


def main() -> None:
    try:
        app()
    except ArchiverError as exc:  # pragma: no cover - commands handle their own errors
        error("unhandled", error_class=type(exc).__name__, error_kind=exc.kind, error=str(exc))
        sys.exit(int(exc.exit_code))


if __name__ == "__main__":  # pragma: no cover
    main()

__all__ = ["app", "event", "main"]
