"""Runner wiring on httpx2.MockTransport: preflight refresh, refresh-check, probe, login."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anyio
import httpx2
import pytest
from mcp.shared.auth import OAuthToken

from tests.fake_http import FakeWisprHttp
from tests.fake_wispr import FakeWisprServer, make_meeting
from tests.harness import FakeClock, RecordingNotifier, make_env
from wispr_archiver.errors import ExitCode, LockBusy
from wispr_archiver.lock import run_lock
from wispr_archiver.runner import run_engine, run_login, run_probe, run_refresh_check
from wispr_archiver.wispr.auth import FileTokenStorage

pytestmark = pytest.mark.anyio

SECRET = "PRIVATE-PROBE-MARKER"


def wispr() -> FakeWisprServer:
    emoji = ("Speaker A:  emoji 😀 " + SECRET + "\n") * 3000
    return FakeWisprServer(
        [
            make_meeting(1, title=f"T {SECRET}", transcript=emoji, content=f"{SECRET}\n" * 30),
            make_meeting(
                2,
                days_ago=1,
                has_transcript=False,
                transcript="",
                summary=f"### Next Steps\n- ({SECRET}) x",
            ),
            make_meeting(3, days_ago=2, finalized=False),
        ]
    )


def seed(path: Path, http: FakeWisprHttp, *, expired: bool = False) -> FileTokenStorage:
    now = datetime.now(UTC) - (timedelta(days=8) if expired else timedelta(0))
    storage = FileTokenStorage(path, now=lambda: now)
    storage.update(client_info=http.register_client())
    tok = http.issue()
    if expired:
        http.valid_access.discard(tok["access_token"])  # the server no longer accepts it
    storage.store_tokens(OAuthToken.model_validate(tok))
    return FileTokenStorage(path)


async def test_expired_token_is_refreshed_before_the_run(tmp_path: Path) -> None:
    http = FakeWisprHttp(wispr())
    env = make_env(tmp_path, http.wispr)
    seed(env.settings.token_path, http, expired=True)
    code = await run_engine(
        env.settings,
        "backfill",
        transport=http.transport,
        notifier=RecordingNotifier(),
        clock=FakeClock(),
    )
    assert code == ExitCode.OK
    assert http.refresh_grants == 1  # proactive refresh, no interactive login
    assert env.main_json(make_meeting(1).id)["transcript_chars"] > 0


async def test_dead_refresh_token_exits_2_with_alert(tmp_path: Path) -> None:
    http = FakeWisprHttp(wispr())
    env = make_env(tmp_path, http.wispr)
    seed(env.settings.token_path, http, expired=True)
    http.valid_refresh.clear()
    notifier = RecordingNotifier()
    code = await run_engine(
        env.settings, "sync", transport=http.transport, notifier=notifier, clock=FakeClock()
    )
    assert code == ExitCode.REAUTH
    assert any("exit 2" in t for t in notifier.sent)


async def test_engine_respects_run_lock(tmp_path: Path) -> None:
    http = FakeWisprHttp(wispr())
    env = make_env(tmp_path, http.wispr)
    seed(env.settings.token_path, http)
    with run_lock(env.settings.lock_path), pytest.raises(LockBusy):
        await run_engine(env.settings, "sync", transport=http.transport)


async def test_refresh_check_rotates_twice(tmp_path: Path) -> None:
    http = FakeWisprHttp(wispr())
    env = make_env(tmp_path, http.wispr)
    seed(env.settings.token_path, http)
    report = await run_refresh_check(env.settings, transport=http.transport)
    assert [s["rotated"] for s in report["steps"]] == [True, True]
    assert all(s["call_after_refresh_ok"] for s in report["steps"])
    assert report["chain_continues"] is True
    assert report["old_refresh_token_replayed"] is False
    assert http.refresh_grants == 2
    text = json.dumps(report)
    assert "rt-" not in text
    assert "at-" not in text


async def test_probe_reports_structure_without_content(tmp_path: Path) -> None:
    http = FakeWisprHttp(wispr())
    env = make_env(tmp_path, http.wispr)
    seed(env.settings.token_path, http)
    out = tmp_path / "probe" / "wispr-probe.json"
    report = await run_probe(env.settings, out, transport=http.transport, pause=0)
    raw = out.read_text(encoding="utf-8")
    assert SECRET not in raw
    assert "Speaker" not in raw
    assert "Synthetic" not in raw
    assert out.stat().st_mode & 0o777 == 0o600
    assert report["errors"] == []
    names = {t["name"] for t in report["tools"]}
    assert "get_meeting" in names
    assert report["search_meetings"]["stats"]["finalized_false"] == 1
    first = report["get_meeting_transcript"][0]["transcript"]
    assert first["header"]
    assert first["marker"]["view"] == "view_transcript"
    (assembled,) = report["full_transcript_assembly"]
    assert assembled["ok"]
    assert assembled["char_unit"] in ("cp", "any")
    assert report["view_content"][0]["content"]["len"] > 0
    assert report["has_transcript_false"]["transcript"] == {"value": "null"}
    assert "no meeting found" in report["not_found"]["error_text"]
    assert report["since_until"]["listed_total"] == 3


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_login_via_real_loopback_callback(tmp_path: Path) -> None:
    http = FakeWisprHttp(wispr())
    port = free_port()
    env = make_env(tmp_path, http.wispr)
    settings = env.settings.replace(login_port=port)

    async def browser(url: str) -> None:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert q["redirect_uri"] == f"http://127.0.0.1:{port}/callback"
        code = "code-xyz"
        http.codes[code] = q["client_id"]

        def hit() -> None:  # the user's browser following the redirect
            with httpx2.Client() as c:
                r = c.get(
                    f"http://127.0.0.1:{port}/callback", params={"code": code, "state": q["state"]}
                )
                assert r.status_code == 200

        await anyio.to_thread.run_sync(hit)

    summary = await run_login(
        settings, open_browser=False, transport=http.transport, redirect_handler=browser
    )
    assert summary["has_refresh_token"] is True
    assert "search_meetings" in summary["tools"]
    data = json.loads(settings.token_path.read_text())
    assert data["endpoints"]["token_endpoint"].endswith("/oauth2/token")
