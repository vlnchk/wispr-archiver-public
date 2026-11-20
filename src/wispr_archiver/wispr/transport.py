"""ToolCaller over the official SDK (streamable HTTP + ClientSession).

Facts about mcp 2.3.0 this adapter is built on (checked against the installed version):
- an HTTP status >= 400 on a tool call reaches the caller as a generic
  MCPError(-32603, "Server returned an error response"): the status and Retry-After are
  only visible to an httpx event hook, so `HttpObserver` records them;
- a network failure or an exception raised inside the OAuth flow (our unattended
  redirect handler raises ReauthRequired) cancels the caller and surfaces as an
  ExceptionGroup when the transport context exits; the session is then dead and is
  reopened on the next call;
- `ClientSession.call_tool` itself issues `tools/list` once per session;
- `OAuthClientProvider` reads the token storage only once per instance, so a fresh
  provider is built for every connection (it then sees tokens refreshed by us).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import anyio
import httpx2
from mcp import ClientSession, MCPError
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.client.streamable_http import StreamableHTTPError, streamable_http_client
from mcp.types import CallToolResult, Implementation, ListToolsResult

from wispr_archiver import __version__
from wispr_archiver.errors import FormatError, ReauthRequired, ToolError
from wispr_archiver.log import warn
from wispr_archiver.wispr.auth import TokenManager
from wispr_archiver.wispr.parse import classify_error_text

INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


class HttpObserver:
    """httpx response hook: remembers the status of the last MCP POST."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.reset()

    def reset(self) -> None:
        self.status: int | None = None
        self.retry_after: float | None = None

    async def __call__(self, response: httpx2.Response) -> None:
        req = response.request
        if req.method == "POST" and str(req.url).rstrip("/") == self.url.rstrip("/"):
            self.status = response.status_code
            self.retry_after = parse_retry_after(response.headers.get("retry-after"))


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        out: list[BaseException] = []
        for sub in exc.exceptions:
            out.extend(_leaves(sub))
        return out
    return [exc]


class McpToolCaller:
    def __init__(
        self,
        url: str,
        *,
        auth_factory: Callable[[], httpx2.Auth] | None = None,
        token_manager: TokenManager | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        read_timeout: float = 120.0,
        forbid_registration: bool = False,
    ) -> None:
        self.url = url
        self.auth_factory = auth_factory
        self.token_manager = token_manager
        self.transport = transport
        self.read_timeout = read_timeout
        self.forbid_registration = forbid_registration
        self.observer = HttpObserver(url)
        self._stack: contextlib.AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self.sessions_opened = 0

    async def __aenter__(self) -> McpToolCaller:
        return self

    async def __aexit__(self, *exc: object) -> None:
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def _connect(self) -> ClientSession:
        stack = contextlib.AsyncExitStack()
        self._stack = stack
        client = httpx2.AsyncClient(
            auth=self.auth_factory() if self.auth_factory else None,
            transport=self.transport,
            timeout=httpx2.Timeout(30.0, read=self.read_timeout),
            event_hooks={"request": [self._guard_request], "response": [self.observer]},
        )
        await stack.enter_async_context(client)
        read, write = await stack.enter_async_context(
            streamable_http_client(self.url, http_client=client)
        )
        session = await stack.enter_async_context(
            ClientSession(
                read,
                write,
                read_timeout_seconds=self.read_timeout,
                client_info=Implementation(name="wispr-archiver", version=__version__),
            )
        )
        await session.initialize()
        self._session = session
        self.sessions_opened += 1
        return session

    async def _guard_request(self, request: httpx2.Request) -> None:
        # Unattended runs must never register an OAuth client (a write on Wispr's side).
        if (
            self.forbid_registration
            and request.method == "POST"
            and request.url.path.rstrip("/").endswith("/register")
        ):
            raise ReauthRequired("client registration is only allowed during login")

    def _map_mcp_error(self, exc: MCPError) -> BaseException:
        status = self.observer.status
        if status == 429:
            return ToolError("HTTP 429", kind="rate_limited", retry_after=self.observer.retry_after)
        if status is not None and (status >= 500 or status == 408):
            return ToolError(f"HTTP {status}", kind="transient")
        if status == 401:
            return ReauthRequired("HTTP 401 from the MCP endpoint")
        if status is not None and status >= 400:
            return ToolError(f"HTTP {status}", kind="tool_error")
        if exc.code in (INVALID_PARAMS, METHOD_NOT_FOUND):
            return FormatError(f"MCP error {exc.code}: tool contract changed")
        kind = classify_error_text(exc.message)
        return ToolError(f"MCP error {exc.code} kind={kind}", kind=kind)

    def _map_group(self, exc: BaseException) -> BaseException:
        leaves = _leaves(exc)
        for leaf in leaves:
            if isinstance(leaf, ReauthRequired):
                return leaf
        for leaf in leaves:
            if isinstance(leaf, MCPError):
                return self._map_mcp_error(leaf)
        for leaf in leaves:
            if isinstance(
                leaf,
                httpx2.HTTPError
                | StreamableHTTPError
                | OAuthFlowError
                | OSError
                | TimeoutError
                | anyio.BrokenResourceError
                | anyio.ClosedResourceError
                | anyio.EndOfStream,
            ):
                return ToolError(f"transport failure ({type(leaf).__name__})", kind="transient")
        return exc

    async def _guarded[T](self, op: Any) -> T:
        """Run `op(session)`; on a transport failure close the session and map the error."""
        try:
            session = self._session or await self._connect()
            self.observer.reset()
            result: T = await op(session)
            return result
        except MCPError as exc:
            raise self._map_mcp_error(exc) from None
        except BaseException as exc:
            stack, self._stack, self._session = self._stack, None, None
            if stack is None:
                raise
            try:
                await stack.__aexit__(type(exc), exc, exc.__traceback__)
            except BaseException as group:
                mapped = self._map_group(group)
                if mapped is group:
                    raise
                warn(
                    "mcp_session_reset",
                    error_class=type(mapped).__name__,
                    error_kind=getattr(mapped, "kind", None),
                )
                raise mapped from None
            raise

    async def _with_reauth_retry[T](self, op: Any) -> T:
        try:
            result: T = await self._guarded(op)
            return result
        except ReauthRequired:
            if self.token_manager is None:
                raise
            # The server asked for interactive auth; try one refresh of our own.
            await self.token_manager.refresh(force=True)
            retried: T = await self._guarded(op)
            return retried

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        async def op(session: ClientSession) -> CallToolResult:
            return await session.call_tool(name, arguments)

        result: CallToolResult = await self._with_reauth_retry(op)
        return result

    async def list_tools(self) -> ListToolsResult:
        async def op(session: ClientSession) -> ListToolsResult:
            return await session.list_tools()

        result: ListToolsResult = await self._with_reauth_retry(op)
        return result


async def unattended_redirect(url: str) -> None:
    raise ReauthRequired("the server requested interactive authorization")


async def unattended_callback() -> Any:
    raise ReauthRequired("the server requested interactive authorization")
