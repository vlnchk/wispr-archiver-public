"""Adapter over the official SDK + OAuth, end to end on httpx2.MockTransport (no sockets)."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from tests.fake_http import MCP_URL, FakeWisprHttp
from tests.fake_wispr import FakeWisprServer, make_meeting
from tests.harness import FakeClock, RecordingNotifier, make_env
from wispr_archiver.archive.store import ArchiveStore
from wispr_archiver.errors import ConfigError, ExitCode, FormatError, ReauthRequired, ToolError
from wispr_archiver.notify import AlertDispatcher
from wispr_archiver.state import State
from wispr_archiver.sync import Archiver
from wispr_archiver.wispr.auth import FileTokenStorage, TokenManager, client_metadata
from wispr_archiver.wispr.client import Deadline, WisprApi
from wispr_archiver.wispr.parse import parse_tool_result
from wispr_archiver.wispr.transport import (
    McpToolCaller,
    parse_retry_after,
    unattended_callback,
    unattended_redirect,
)

pytestmark = pytest.mark.anyio


def seeded(tmp_path: Path, http: FakeWisprHttp) -> FileTokenStorage:
    storage = FileTokenStorage(tmp_path / "tok" / "token.json")
    storage.update(client_info=http.register_client())
    storage.store_tokens(OAuthToken.model_validate(http.issue()))
    return storage


def caller(
    http: FakeWisprHttp, storage: FileTokenStorage, *, manager: bool = True
) -> McpToolCaller:
    def provider() -> OAuthClientProvider:
        return OAuthClientProvider(
            server_url=MCP_URL,
            client_metadata=client_metadata(33418),
            storage=storage,
            redirect_handler=unattended_redirect,
            callback_handler=unattended_callback,
        )

    tm = TokenManager(storage, transport=http.transport) if manager else None
    return McpToolCaller(
        MCP_URL,
        auth_factory=provider,
        token_manager=tm,
        transport=http.transport,
        forbid_registration=True,
    )


@pytest.fixture
def http() -> FakeWisprHttp:
    return FakeWisprHttp(FakeWisprServer([make_meeting(1), make_meeting(2, days_ago=2)]))


async def search(c: McpToolCaller) -> dict[str, Any]:
    return parse_tool_result(await c.call_tool("search_meetings", {"limit": 5}), tool="s")


# --- adapter ----------------------------------------------------------------------------


async def test_call_ok_and_session_reused(tmp_path: Path, http: FakeWisprHttp) -> None:
    async with caller(http, seeded(tmp_path, http)) as c:
        assert len((await search(c))["meetings"]) == 2
        await search(c)
        assert c.sessions_opened == 1


async def test_429_is_rate_limited_with_retry_after(tmp_path: Path, http: FakeWisprHttp) -> None:
    async with caller(http, seeded(tmp_path, http)) as c:
        await search(c)
        http.fault = "429"
        with pytest.raises(ToolError) as exc:
            await search(c)
        assert (exc.value.kind, exc.value.retry_after) == ("rate_limited", 75.0)
        await search(c)
        assert c.sessions_opened == 1  # an HTTP error does not kill the session


async def test_5xx_is_transient(tmp_path: Path, http: FakeWisprHttp) -> None:
    async with caller(http, seeded(tmp_path, http)) as c:
        http.fault = "500"
        with pytest.raises(ToolError) as exc:
            await search(c)
        assert exc.value.kind == "transient"


async def test_connection_failure_resets_session(tmp_path: Path, http: FakeWisprHttp) -> None:
    async with caller(http, seeded(tmp_path, http)) as c:
        await search(c)
        http.fault = "connect"
        with pytest.raises(ToolError) as exc:
            await search(c)
        assert exc.value.kind == "transient"
        await search(c)
        assert c.sessions_opened == 2


async def test_invalid_params_is_format_error(tmp_path: Path, http: FakeWisprHttp) -> None:
    async with caller(http, seeded(tmp_path, http)) as c:
        http.fault = "invalid_params"
        with pytest.raises(FormatError):
            await search(c)


async def test_revoked_access_token_triggers_one_own_refresh(
    tmp_path: Path, http: FakeWisprHttp
) -> None:
    storage = seeded(tmp_path, http)
    before = storage.read()["tokens"]["refresh_token"]
    async with caller(http, storage) as c:
        await search(c)
        http.fault = "revoke_access"
        await search(c)  # 401 -> SDK asks for interactive auth -> our refresh -> retry
    assert http.refresh_grants == 1
    assert storage.read()["tokens"]["refresh_token"] != before


async def test_revoked_everything_is_reauth(tmp_path: Path, http: FakeWisprHttp) -> None:
    storage = seeded(tmp_path, http)
    http.valid_access.clear()
    http.valid_refresh.clear()
    async with caller(http, storage) as c:
        with pytest.raises(ReauthRequired):
            await search(c)


async def test_unattended_storage_refuses_client_registration(
    tmp_path: Path, http: FakeWisprHttp
) -> None:
    storage = FileTokenStorage(tmp_path / "t.json", allow_registration=False)
    with pytest.raises(ReauthRequired, match="login"):
        await storage.set_client_info(OAuthClientInformationFull(client_id="x"))
    unregistered = FileTokenStorage(tmp_path / "u.json", allow_registration=False)
    unregistered.store_tokens(OAuthToken(access_token="a", refresh_token="r", expires_in=10))
    before = len(http.clients)
    async with caller(http, unregistered, manager=False) as c:
        with pytest.raises(ReauthRequired):
            await search(c)  # SDK tries DCR after 401 -> refused, nothing registered
    assert len(http.clients) == before


async def test_without_token_manager_401_is_reauth(tmp_path: Path, http: FakeWisprHttp) -> None:
    storage = seeded(tmp_path, http)
    http.valid_access.clear()
    async with caller(http, storage, manager=False) as c:
        with pytest.raises(ReauthRequired):
            await search(c)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("75", 75.0), (None, None), ("garbage", None), ("Wed, 18 Nov 2026 12:01:00 GMT", 60.0)],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    assert parse_retry_after(value, now) == expected


# --- storage ----------------------------------------------------------------------------


def test_storage_file_modes_and_atomicity(tmp_path: Path, http: FakeWisprHttp) -> None:
    storage = seeded(tmp_path, http)
    assert storage.path.stat().st_mode & 0o777 == 0o600
    assert storage.path.parent.stat().st_mode & 0o777 == 0o700
    leftovers = [p.name for p in storage.path.parent.iterdir()]
    assert sorted(leftovers) == ["token.json", "token.json.lock"]
    storage.path.chmod(0o644)
    with pytest.raises(ConfigError, match="permissions"):
        storage.read()


def test_storage_refuses_git_worktree(tmp_path: Path) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(ConfigError, match="outside"):
        FileTokenStorage(tmp_path / "repo" / "secrets" / "token.json")


def test_storage_keeps_refresh_token_if_not_rotated(tmp_path: Path) -> None:
    s = FileTokenStorage(tmp_path / "t.json")
    s.store_tokens(OAuthToken(access_token="a1", refresh_token="r1", expires_in=10))
    s.store_tokens(OAuthToken(access_token="a2", expires_in=10))
    assert s.read()["tokens"]["refresh_token"] == "r1"
    assert s.read()["expires_at"]


async def test_sdk_protocol_methods(tmp_path: Path, http: FakeWisprHttp) -> None:
    s = seeded(tmp_path, http)
    tok = await s.get_tokens()
    assert tok is not None
    assert tok.refresh_token
    info = await s.get_client_info()
    assert info is not None
    assert info.client_id == "client-1"
    ignoring = FileTokenStorage(s.path, ignore_stored_tokens=True)
    assert await ignoring.get_tokens() is None
    assert await ignoring.get_client_info() is not None


# --- token manager ----------------------------------------------------------------------


async def test_refresh_respects_margin_and_force(tmp_path: Path, http: FakeWisprHttp) -> None:
    storage = seeded(tmp_path, http)
    tm = TokenManager(storage, transport=http.transport)
    out = await tm.refresh(margin=timedelta(hours=24))
    assert not out.refreshed  # 7 days left
    later = TokenManager(
        storage,
        transport=http.transport,
        now=lambda: datetime.now(UTC) + timedelta(days=6, hours=1),
    )
    out = await later.refresh(margin=timedelta(hours=24))
    assert out.refreshed
    assert out.rotated
    assert storage.read()["endpoints"]["token_endpoint"].endswith("/oauth2/token")
    out2 = await tm.refresh(force=True)
    assert out2.old_sha8 == out.new_sha8  # the chain continues from the stored token


async def test_24h_tokens_are_not_refreshed_every_run(tmp_path: Path, http: FakeWisprHttp) -> None:
    http.expires_in = 86_400  # observed live 2026-11-18
    storage = seeded(tmp_path, http)
    tm = TokenManager(storage, transport=http.transport)
    out = await tm.refresh(margin=timedelta(hours=24))
    assert not out.refreshed  # 24 h left > 6 h effective margin
    assert http.refresh_grants == 0
    late = TokenManager(
        storage, transport=http.transport, now=lambda: datetime.now(UTC) + timedelta(hours=19)
    )
    assert (await late.refresh(margin=timedelta(hours=24))).refreshed


def test_effective_margin() -> None:
    from wispr_archiver.wispr.auth import effective_margin

    day = {"obtained_at": "2026-11-18T00:00:00+00:00", "expires_at": "2026-11-19T00:00:00+00:00"}
    week = {"obtained_at": "2026-11-18T00:00:00+00:00", "expires_at": "2026-11-25T00:00:00+00:00"}
    assert effective_margin(day, timedelta(hours=24)) == timedelta(hours=6)
    assert effective_margin(week, timedelta(hours=24)) == timedelta(hours=24)
    assert effective_margin({}, timedelta(hours=24)) == timedelta(hours=24)


def test_storage_refuses_writable_directory(tmp_path: Path) -> None:
    s = FileTokenStorage(tmp_path / "d" / "t.json")
    s.store_tokens(OAuthToken(access_token="a", refresh_token="r", expires_in=10))
    (tmp_path / "d").chmod(0o777)
    with pytest.raises(ConfigError, match="directory"):
        s.read()
    (tmp_path / "d").chmod(0o755)  # readable is fine: the file itself is 0600
    assert s.read()["tokens"]["access_token"] == "a"


async def test_refresh_without_rotation(tmp_path: Path) -> None:
    http = FakeWisprHttp(FakeWisprServer([]), rotate_refresh=False)
    storage = seeded(tmp_path, http)
    out = await TokenManager(storage, transport=http.transport).refresh(force=True)
    assert out.refreshed
    assert not out.rotated


async def test_refresh_invalid_grant_is_reauth(tmp_path: Path, http: FakeWisprHttp) -> None:
    storage = seeded(tmp_path, http)
    http.valid_refresh.clear()
    with pytest.raises(ReauthRequired):
        await TokenManager(storage, transport=http.transport).refresh(force=True)


async def test_refresh_server_error_is_transient(tmp_path: Path, http: FakeWisprHttp) -> None:
    import httpx2

    storage = seeded(tmp_path, http)
    await TokenManager(storage, transport=http.transport).discover()

    def boom(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503)

    with pytest.raises(ToolError) as exc:
        await TokenManager(storage, transport=httpx2.MockTransport(boom)).refresh(force=True)
    assert exc.value.kind == "transient"


async def test_refresh_without_login_is_reauth(tmp_path: Path) -> None:
    with pytest.raises(ReauthRequired, match="login"):
        await TokenManager(FileTokenStorage(tmp_path / "none.json")).refresh(force=True)


# --- login flow (interactive handlers simulated) ----------------------------------------


async def test_login_flow_registers_and_stores_tokens(tmp_path: Path, http: FakeWisprHttp) -> None:
    storage = FileTokenStorage(tmp_path / "tok" / "token.json", ignore_stored_tokens=True)
    seen: dict[str, str] = {}

    async def redirect(url: str) -> None:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert q["code_challenge_method"] == "S256"
        assert q["redirect_uri"] == "http://127.0.0.1:33418/callback"
        code = "code-" + q["state"][:6]
        http.codes[code] = q["client_id"]
        seen.update(code=code, state=q["state"])

    async def callback() -> AuthorizationCodeResult:
        return AuthorizationCodeResult(code=seen["code"], state=seen["state"])

    provider = OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=client_metadata(33418),
        storage=storage,
        redirect_handler=redirect,
        callback_handler=callback,
    )
    async with McpToolCaller(MCP_URL, auth_factory=lambda: provider, transport=http.transport) as c:
        tools = await c.list_tools()
    assert {t.name for t in tools.tools} >= {"search_meetings", "get_meeting"}
    data = json.loads(storage.path.read_text())
    assert data["tokens"]["refresh_token"].startswith("rt-")
    assert data["client_info"]["client_id"].startswith("client-")
    assert storage.path.stat().st_mode & 0o777 == 0o600


# --- engine over the real adapter -------------------------------------------------------


async def test_engine_end_to_end_over_http(tmp_path: Path, http: FakeWisprHttp) -> None:
    env = make_env(tmp_path, http.wispr)
    storage = seeded(tmp_path, http)
    clock = FakeClock()

    async def run() -> tuple[ExitCode, int]:
        state = State.open(env.settings.state_path)
        async with caller(http, storage) as c:
            api = WisprApi(c, clock, env.settings, Deadline(clock, 1200))
            arch = Archiver(
                settings=env.settings,
                api=api,
                store=ArchiveStore(env.archive, env.settings.tz, shrink_threshold=0.1),
                state=state,
                clock=clock,
                alerts=AlertDispatcher(state, RecordingNotifier(), dedup_hours=6),
                rng=random.Random(0),
            )
            code = await arch.run("backfill")
        state.close()
        return code, arch.summary.bytes_written

    code, written = await run()
    assert code == ExitCode.OK
    assert written > 0
    assert (
        env.main_json(make_meeting(1).id)["transcript_text"]
        == http.wispr.meetings[make_meeting(1).id].transcript
    )
    code, written = await run()
    assert (code, written) == (ExitCode.OK, 0)
