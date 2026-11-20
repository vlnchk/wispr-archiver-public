"""HTTP-level fake of the Wispr MCP endpoint and its OAuth server for httpx2.MockTransport.

No sockets: the official SDK transport, OAuth provider and our adapter run unchanged on
top of it. Tool calls are delegated to FakeWisprServer. Hosts/paths mirror the observed
metadata in docs/wispr-mcp-contract.md. All tokens are random test values.
"""

from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import parse_qs

import httpx2

from tests.fake_wispr import FakeWisprServer

MCP_URL = "https://api.wisprflow.ai/connect/mcp"
PRM = "https://api.wisprflow.ai/.well-known/oauth-protected-resource"
AS = "https://mcp-auth.wisprflow.com"

TOOLS = [
    {"name": n, "inputSchema": {"type": "object"}}
    for n in ("search_meetings", "get_meeting", "get_meeting_participants_enriched")
]


class FakeWisprHttp:
    def __init__(
        self, wispr: FakeWisprServer, *, require_auth: bool = True, rotate_refresh: bool = True
    ) -> None:
        self.wispr = wispr
        self.require_auth = require_auth
        self.rotate_refresh = rotate_refresh
        self.valid_access: set[str] = set()
        self.valid_refresh: set[str] = set()
        self.codes: dict[str, str] = {}  # code -> client_id
        self.clients: dict[str, dict[str, Any]] = {}
        self.fault: str | None = None  # "429", "500", "connect", "invalid_params", "revoke_access"
        self.fault_once = True
        self.requests: list[tuple[str, str]] = []
        self.refresh_grants = 0
        self.expires_in = 604_800

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    # --- helpers --------------------------------------------------------------------

    def issue(self) -> dict[str, Any]:
        access = "at-" + secrets.token_hex(8)
        refresh = "rt-" + secrets.token_hex(8)
        self.valid_access.add(access)
        self.valid_refresh.add(refresh)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": refresh,
            "scope": "openid offline_access",
        }

    def register_client(self, client_id: str = "client-1") -> dict[str, Any]:
        info = {
            "client_id": client_id,
            "token_endpoint_auth_method": "none",
            "redirect_uris": ["http://127.0.0.1:33418/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "openid offline_access",
            "client_name": "wispr-archiver",
        }
        self.clients[client_id] = info
        return info

    def _take_fault(self) -> str | None:
        f = self.fault
        if f and self.fault_once:
            self.fault = None
        return f

    # --- dispatcher -----------------------------------------------------------------

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        self.requests.append((request.method, url))
        if url == PRM:
            return httpx2.Response(
                200,
                json={
                    "resource": MCP_URL,
                    "authorization_servers": [AS],
                    "bearer_methods_supported": ["header"],
                    "scopes_supported": ["openid", "offline_access"],
                },
            )
        if url == f"{AS}/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": AS,
                    "authorization_endpoint": f"{AS}/oauth2/authorize",
                    "token_endpoint": f"{AS}/oauth2/token",
                    "registration_endpoint": f"{AS}/oauth2/register",
                    "revocation_endpoint": f"{AS}/oauth2/revoke",
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "response_types_supported": ["code"],
                    "scopes_supported": ["openid", "offline_access"],
                },
            )
        if url.startswith(AS) and "/.well-known/" in url:
            return httpx2.Response(404)
        if url == f"{AS}/oauth2/register":
            body = json.loads(request.content)
            info = {**body, "client_id": "client-" + secrets.token_hex(4)}
            self.clients[info["client_id"]] = info
            return httpx2.Response(201, json=info)
        if url == f"{AS}/oauth2/token":
            return self._token(request)
        if url.startswith(MCP_URL):
            return await self._mcp(request)
        return httpx2.Response(404)

    def _token(self, request: httpx2.Request) -> httpx2.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("grant_type") == "authorization_code":
            if form.get("code") not in self.codes:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            del self.codes[form["code"]]
            return httpx2.Response(200, json=self.issue())
        if form.get("grant_type") == "refresh_token":
            self.refresh_grants += 1
            rt = form.get("refresh_token")
            if rt not in self.valid_refresh or form.get("client_id") not in self.clients:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            if form.get("resource") != MCP_URL:
                return httpx2.Response(400, json={"error": "invalid_target"})
            tok = self.issue()
            if self.rotate_refresh:
                self.valid_refresh.discard(rt)
            else:
                self.valid_refresh.discard(tok["refresh_token"])
                del tok["refresh_token"]
            return httpx2.Response(200, json=tok)
        return httpx2.Response(400, json={"error": "unsupported_grant_type"})

    async def _mcp(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "DELETE":
            return httpx2.Response(204)
        if request.method == "GET":
            return httpx2.Response(405)
        if self.require_auth:
            auth = request.headers.get("authorization", "")
            token = auth.removeprefix("Bearer ").strip()
            if token not in self.valid_access:
                return httpx2.Response(
                    401,
                    headers={
                        "www-authenticate": f'Bearer resource_metadata="{PRM}", '
                        'scope="openid offline_access"'
                    },
                )
        msg = json.loads(request.content)
        if "id" not in msg:
            return httpx2.Response(202)
        method = msg["method"]
        if method == "initialize":
            return self._rpc(
                msg["id"],
                {
                    "protocolVersion": msg["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-wispr", "version": "0"},
                },
                headers={"mcp-session-id": "sess-" + secrets.token_hex(4)},
            )
        if method == "tools/list":
            return self._rpc(msg["id"], {"tools": TOOLS})
        if method == "tools/call":
            fault = self._take_fault()
            if fault == "429":
                return httpx2.Response(429, headers={"retry-after": "75"}, text="Too many requests")
            if fault == "500":
                return httpx2.Response(502, text="bad gateway")
            if fault == "connect":
                raise httpx2.ConnectError("connection refused", request=request)
            if fault == "invalid_params":
                return self._rpc_error(msg["id"], -32602, "Invalid params")
            if fault == "revoke_access":
                self.valid_access.clear()
                return httpx2.Response(
                    401, headers={"www-authenticate": f'Bearer resource_metadata="{PRM}"'}
                )
            params = msg["params"]
            result = await self.wispr.call_tool(params["name"], params.get("arguments") or {})
            return self._rpc(
                msg["id"], result.model_dump(by_alias=True, mode="json", exclude_none=True)
            )
        return self._rpc_error(msg["id"], -32601, "Method not found")

    @staticmethod
    def _rpc(
        msg_id: Any, result: dict[str, Any], headers: dict[str, str] | None = None
    ) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "application/json", **(headers or {})},
            json={"jsonrpc": "2.0", "id": msg_id, "result": result},
        )

    @staticmethod
    def _rpc_error(msg_id: Any, code: int, message: str) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}},
        )
