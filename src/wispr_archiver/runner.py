"""Wiring for the commands that talk to Wispr: login, probe, refresh-check, backfill/sync/reconcile.

Every command here holds the single-instance run lock because each of them may
rotate the refresh token.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
from mcp.client.auth import OAuthClientProvider

from wispr_archiver import WISPR_MCP_URL
from wispr_archiver.archive.store import ArchiveStore
from wispr_archiver.config import Settings
from wispr_archiver.errors import ExitCode
from wispr_archiver.lock import run_lock
from wispr_archiver.log import event
from wispr_archiver.login import LoopbackCallback, make_redirect_handler
from wispr_archiver.notify import AlertDispatcher, Notifier, make_notifier
from wispr_archiver.probe import Probe
from wispr_archiver.state import State
from wispr_archiver.sync import Archiver, Command
from wispr_archiver.wispr.auth import FileTokenStorage, TokenManager, client_metadata
from wispr_archiver.wispr.client import Clock, Deadline, SystemClock, WisprApi
from wispr_archiver.wispr.transport import McpToolCaller, unattended_callback, unattended_redirect


def unattended_caller(
    s: Settings,
    storage: FileTokenStorage,
    manager: TokenManager,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> McpToolCaller:
    def provider() -> OAuthClientProvider:
        return OAuthClientProvider(
            server_url=WISPR_MCP_URL,
            client_metadata=client_metadata(s.login_port),
            storage=storage,
            redirect_handler=unattended_redirect,
            callback_handler=unattended_callback,
        )

    return McpToolCaller(
        WISPR_MCP_URL,
        auth_factory=provider,
        token_manager=manager,
        transport=transport,
        forbid_registration=True,
    )


async def run_engine(
    s: Settings,
    command: Command,
    *,
    dry_run: bool = False,
    transport: httpx2.AsyncBaseTransport | None = None,
    notifier: Notifier | None = None,
    clock: Clock | None = None,
) -> ExitCode:
    with run_lock(s.lock_path):
        storage = FileTokenStorage(s.token_path, allow_registration=False)
        manager = TokenManager(storage, transport=transport)
        clock = clock or SystemClock()
        state = State.open(s.state_path, dry_run=dry_run)
        try:
            async with unattended_caller(s, storage, manager, transport=transport) as caller:
                api = WisprApi(caller, clock, s, Deadline(clock, s.run_timeout_seconds))

                async def preflight() -> None:
                    await manager.refresh(margin=timedelta(hours=s.refresh_margin_hours))

                archiver = Archiver(
                    settings=s,
                    api=api,
                    store=ArchiveStore(
                        s.archive_dir, s.tz, shrink_threshold=s.shrink_threshold, dry_run=dry_run
                    ),
                    state=state,
                    clock=clock,
                    alerts=AlertDispatcher(
                        state,
                        notifier or make_notifier(s, dry_run=dry_run),
                        dedup_hours=s.alert_dedup_hours,
                    ),
                    dry_run=dry_run,
                    preflight=preflight,
                )
                return await archiver.run(command)
        finally:
            state.close()


async def run_login(
    s: Settings,
    *,
    open_browser: bool,
    transport: httpx2.AsyncBaseTransport | None = None,
    callback_factory: Callable[[int], LoopbackCallback] = LoopbackCallback,
    redirect_handler: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    with run_lock(s.lock_path):
        storage = FileTokenStorage(s.token_path, ignore_stored_tokens=True)
        metadata = client_metadata(s.login_port)
        storage.drop_client_info_unless(metadata)
        callback = callback_factory(s.login_port)
        callback.start()
        try:
            provider = OAuthClientProvider(
                server_url=WISPR_MCP_URL,
                client_metadata=metadata,
                storage=storage,
                redirect_handler=redirect_handler
                or make_redirect_handler(open_browser=open_browser),
                callback_handler=callback.wait,
            )
            async with McpToolCaller(
                WISPR_MCP_URL, auth_factory=lambda: provider, transport=transport
            ) as caller:
                tools = await caller.list_tools()
        finally:
            callback.stop()
        await TokenManager(storage, transport=transport).discover()
        data = storage.read()
        tok = data.get("tokens") or {}
        summary = {
            "token_file": str(s.token_path),
            "tools": sorted(t.name for t in tools.tools),
            "expires_at": data.get("expires_at"),
            "scope": tok.get("scope"),
            "has_refresh_token": bool(tok.get("refresh_token")),
        }
        event("login_ok", expires_at=data.get("expires_at"), count=len(tools.tools))
        return summary


async def run_refresh_check(
    s: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """Force refresh twice, calling a read-only tool after each.

    The superseded refresh token is never presented again: replaying it could trigger
    refresh-token reuse detection and revoke the whole chain.
    """
    with run_lock(s.lock_path):
        storage = FileTokenStorage(s.token_path, allow_registration=False)
        manager = TokenManager(storage, transport=transport)
        steps = []
        for step in (1, 2):
            out = await manager.refresh(force=True)
            async with unattended_caller(s, storage, manager, transport=transport) as caller:
                result = await caller.call_tool("search_meetings", {"limit": 1})
            steps.append(
                {
                    "step": step,
                    "rotated": out.rotated,
                    "expires_in": out.expires_in,
                    "scope": out.scope,
                    "old_refresh_sha8": out.old_sha8,
                    "new_refresh_sha8": out.new_sha8,
                    "call_after_refresh_ok": not result.is_error,
                }
            )
        report = {
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "steps": steps,
            "chain_continues": steps[1]["old_refresh_sha8"] == steps[0]["new_refresh_sha8"],
            "expires_at": storage.read().get("expires_at"),
            "old_refresh_token_replayed": False,
        }
        event("refresh_check_ok", rotated=all(x["rotated"] for x in steps))
        return report


async def run_probe(
    s: Settings,
    out: Path,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    pause: float = 1.0,
) -> dict[str, Any]:
    with run_lock(s.lock_path):
        storage = FileTokenStorage(s.token_path, allow_registration=False)
        manager = TokenManager(storage, transport=transport)
        await manager.refresh(margin=timedelta(hours=s.refresh_margin_hours))
        async with unattended_caller(s, storage, manager, transport=transport) as caller:
            report = await Probe(caller, caller.observer, pause=pause).run()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out.chmod(0o600)
        event("probe_written", count=len(report.get("errors", [])))
        return report
