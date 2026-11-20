"""Typed, rate-limited access to the Wispr MCP tools.

`ToolCaller` is the seam between the engine and the transport: the real adapter wraps the
official SDK session (M2), tests use FakeWisprServer. The transport maps HTTP 429 to
ToolError(kind="rate_limited", retry_after=...) and network/5xx failures to
ToolError(kind="transient").
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Protocol

import anyio
from mcp.types import CallToolResult

from wispr_archiver.config import Settings
from wispr_archiver.errors import CircuitOpen, FormatError, RunTimeout, ToolError
from wispr_archiver.log import warn
from wispr_archiver.wispr.models import MeetingPayload, ParticipantsPayload, SearchPage, validate
from wispr_archiver.wispr.parse import parse_tool_result

#: Read-only tools we are allowed to call. Anything else is refused.
ALLOWED_TOOLS = frozenset(
    {
        "search_meetings",
        "get_meeting",
        "get_meeting_participants_enriched",
        "list_folders",
        "list_meeting_series",
    }
)


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...
    def utcnow(self) -> datetime: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await anyio.sleep(seconds)

    def utcnow(self) -> datetime:
        return datetime.now(UTC)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Deadline:
    def __init__(self, clock: Clock, seconds: float) -> None:
        self.clock = clock
        self.end = clock.monotonic() + seconds

    def remaining(self) -> float:
        return self.end - self.clock.monotonic()

    def check(self) -> None:
        if self.remaining() <= 0:
            raise RunTimeout("run timeout exceeded")

    async def sleep(self, seconds: float) -> None:
        if seconds >= self.remaining():
            raise RunTimeout(f"sleeping {seconds:.0f}s would exceed the run timeout")
        await self.clock.sleep(seconds)


class WisprApi:
    def __init__(
        self, caller: ToolCaller, clock: Clock, settings: Settings, deadline: Deadline
    ) -> None:
        self.caller = caller
        self.clock = clock
        self.settings = settings
        self.deadline = deadline
        self._interval = 1.0 / settings.requests_per_second
        self._last_call: float | None = None
        self._consecutive_transient = 0
        self.calls = 0

    async def _throttle(self) -> None:
        if self._last_call is not None:
            wait = self._last_call + self._interval - self.clock.monotonic()
            if wait > 0:
                await self.deadline.sleep(wait)
        self._last_call = self.clock.monotonic()

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool not in ALLOWED_TOOLS:
            raise ValueError(f"tool {tool} is not on the read-only allowlist")
        retries = self.settings.rate_limit_retries
        for i in range(retries + 1):
            self.deadline.check()
            await self._throttle()
            self.calls += 1
            try:
                payload = parse_tool_result(await self.caller.call_tool(tool, args), tool=tool)
            except ToolError as exc:
                if exc.kind == "transient":
                    self._consecutive_transient += 1
                    if self._consecutive_transient >= self.settings.circuit_breaker_failures:
                        raise CircuitOpen(
                            f"{self._consecutive_transient} consecutive transient failures"
                        ) from exc
                if exc.kind == "rate_limited" and i < retries:
                    pause = max(self.settings.rate_limit_pause_seconds, exc.retry_after or 0.0)
                    warn(
                        "rate_limited",
                        tool=tool,
                        sleep_s=pause,
                        attempt=i + 1,
                        retry_after_s=exc.retry_after,
                    )
                    await self.deadline.sleep(pause)
                    continue
                raise
            self._consecutive_transient = 0
            return payload
        raise AssertionError("unreachable")  # pragma: no cover

    async def sleep(self, seconds: float) -> None:
        await self.deadline.sleep(seconds)

    # --- tools --------------------------------------------------------------------------

    async def search_meetings(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> SearchPage:
        args: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            args["cursor"] = cursor
        if since is not None:
            args["since"] = since
        if until is not None:
            args["until"] = until
        return validate(
            SearchPage, await self.call("search_meetings", args), tool="search_meetings"
        )

    async def get_meeting(
        self,
        meeting_id: str,
        *,
        view_transcript: tuple[int, int] | None = None,
        view_content: tuple[int, int] | None = None,
    ) -> MeetingPayload:
        args: dict[str, Any] = {"meeting_id": meeting_id}
        if view_transcript is not None:
            args["view_transcript"] = {
                "start_char": view_transcript[0],
                "char_limit": view_transcript[1],
            }
        if view_content is not None:
            args["view_content"] = {"start_char": view_content[0], "char_limit": view_content[1]}
        payload = validate(MeetingPayload, await self.call("get_meeting", args), tool="get_meeting")
        if payload.id != meeting_id:
            raise FormatError("get_meeting: returned a different meeting id")
        return payload

    async def get_participants(self, meeting_id: str) -> ParticipantsPayload:
        payload = validate(
            ParticipantsPayload,
            await self.call("get_meeting_participants_enriched", {"meeting_id": meeting_id}),
            tool="get_meeting_participants_enriched",
        )
        if payload.meeting_id != meeting_id:
            raise FormatError("get_meeting_participants_enriched: different meeting id")
        return payload
