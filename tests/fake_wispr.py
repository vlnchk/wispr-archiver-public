"""In-process FakeWisprServer: implements the ToolCaller protocol with synthetic data in the
exact observed formats (docs/wispr-mcp-contract.md). Supports fault injection via `hook`.
"""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.types import CallToolResult, TextContent

from tests.wispr_format import FOOTER, HEADER, Unit, chunk

Hook = Callable[[str, dict[str, Any], int], CallToolResult | BaseException | None]


def ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class FakeMeeting:
    id: str
    title: str
    start: str
    modified_at: str
    end: str | None = None
    finalized: bool = True
    has_transcript: bool = True
    transcript: str = ""
    content: str = ""
    summary: str = ""
    todos: list[Any] = field(default_factory=list)
    attendees: list[Any] = field(default_factory=list)
    participants: list[dict[str, Any]] = field(default_factory=list)
    shared: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    omit_keys: set[str] = field(default_factory=set)  # keys missing from get_meeting


def text_result(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))])


def error_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


class FakeWisprServer:
    def __init__(
        self,
        meetings: list[FakeMeeting] | None = None,
        *,
        unit: Unit = "cp",
        truncate_at: int = 1000,
    ) -> None:
        self.meetings: dict[str, FakeMeeting] = {m.id: m for m in meetings or []}
        self.unit: Unit = unit
        self.truncate_at = truncate_at
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.hook: Hook | None = None
        self.header = HEADER
        self.clock_base = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
        self._bumps = 0

    # --- data management --------------------------------------------------------------

    def add(self, m: FakeMeeting) -> None:
        self.meetings[m.id] = m

    def touch(self, meeting_id: str, **changes: Any) -> FakeMeeting:
        """Change a meeting and bump modified_at (strictly newer than everything else)."""
        m = self.meetings[meeting_id]
        for k, v in changes.items():
            setattr(m, k, v)
        newest = max(_parse(x.modified_at) for x in self.meetings.values())
        self._bumps += 1
        m.modified_at = ts(newest + timedelta(minutes=self._bumps))
        return m

    def delete(self, meeting_id: str) -> None:
        del self.meetings[meeting_id]

    def count(self, tool: str) -> int:
        return sum(1 for name, _ in self.calls if name == tool)

    # --- ToolCaller -------------------------------------------------------------------

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        self.calls.append((name, copy.deepcopy(arguments)))
        if self.hook is not None:
            r = self.hook(name, arguments, len(self.calls))
            if isinstance(r, BaseException):
                raise r
            if r is not None:
                return r
        handler = {
            "search_meetings": self._search,
            "get_meeting": self._get,
            "get_meeting_participants_enriched": self._participants,
        }.get(name)
        if handler is None:
            return error_result(f"Unknown tool {name}")
        return handler(arguments)

    # --- tools ------------------------------------------------------------------------

    def _listed(self, m: FakeMeeting) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": m.id,
            "title": m.title,
            "content_excerpt": m.content[:40],
            "finalized": m.finalized,
            "has_transcript": m.has_transcript,
            "attendees": m.attendees,
            "start": m.start,
            "end": m.end,
            "modified_at": m.modified_at,
            "share_link": f"https://notes.wisprflow.ai/shared/SYNTH{m.id[:4]}",
        }
        if m.shared:
            d["shared"] = True
            d["owner"] = {"name": "Synthetic Owner"}
        return d

    def _search(self, args: dict[str, Any]) -> CallToolResult:
        items = sorted(self.meetings.values(), key=lambda m: (m.modified_at, m.id), reverse=True)
        if "since" in args:
            items = [m for m in items if _parse(m.start) >= _parse(args["since"])]
        if "until" in args:
            items = [m for m in items if _parse(m.start) < _parse(args["until"])]
        truncated = len(items) > self.truncate_at
        items = items[: self.truncate_at]
        offset = 0
        if "cursor" in args:
            offset = int(base64.b64decode(args["cursor"] + "==").decode().split("|")[2])
        limit = int(args.get("limit", 25))
        page = items[offset : offset + limit]
        has_more = offset + limit < len(items)
        payload: dict[str, Any] = {
            "meetings": [self._listed(m) for m in page],
            "count": len(page),
            "has_more": has_more,
            "note": "These results cover only meetings captured in Wispr Flow. (synthetic)",
        }
        if has_more:
            last = page[-1]
            raw = f"{last.modified_at.rstrip('Z')}|{last.id}|{offset + limit}"
            payload["next_cursor"] = base64.b64encode(raw.encode()).decode().rstrip("=")
            payload["more"] = f"...({len(items) - offset - limit} more meetings)..."
        if truncated:
            payload["truncated"] = True
        return text_result(payload)

    def _get(self, args: dict[str, Any]) -> CallToolResult:
        m = self.meetings.get(args["meeting_id"])
        if m is None:
            return error_result(f"no meeting found for id: {args['meeting_id']}")
        payload: dict[str, Any] = {
            "id": m.id,
            "title": m.title,
            "summary": m.summary,
            "finalized": m.finalized,
            "has_transcript": m.has_transcript,
            "attendees": m.attendees,
            "todos": m.todos,
            "start": m.start,
            "end": m.end,
            "modified_at": m.modified_at,
            "share_link": f"https://notes.wisprflow.ai/shared/SYNTH{m.id[:4]}",
            **m.extra,
        }
        if m.shared:
            payload["shared"] = True
        for key in m.omit_keys:
            payload.pop(key, None)
        if "view_content" in args:
            v = args["view_content"]
            payload["content"] = chunk(
                m.content,
                v["start_char"],
                v["char_limit"],
                view="view_content",
                wrapped=False,
                unit=self.unit,
            )
        else:
            payload["content"] = ""
        if "view_transcript" in args:
            v = args["view_transcript"]
            if not m.has_transcript:
                payload["transcript"] = None
            else:
                c = chunk(m.transcript, v["start_char"], v["char_limit"], unit=self.unit)
                if self.header != HEADER:
                    c = self.header + c[len(HEADER) :]
                payload["transcript"] = c
        return text_result(payload)

    def _participants(self, args: dict[str, Any]) -> CallToolResult:
        m = self.meetings.get(args["meeting_id"])
        if m is None:
            return error_result(f"no meeting found for id: {args['meeting_id']}")
        return text_result(
            {
                "meeting_id": m.id,
                "user": {
                    "name": "Owner Name",
                    "email": "owner@example.com",
                    "domain": "example.com",
                },
                "participants": m.participants,
                "mentioned_contacts": [],
                "note": "synthetic note",
            }
        )


def make_meeting(
    n: int,
    *,
    days_ago: float = 0,
    transcript_len: int = 500,
    title: str | None = None,
    base: datetime | None = None,
    **kw: Any,
) -> FakeMeeting:
    from tests.wispr_format import transcript_text

    base = base or datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    start = base - timedelta(days=days_ago, hours=2) + timedelta(seconds=n)
    defaults: dict[str, Any] = {
        "end": ts(start + timedelta(hours=1)),
        "transcript": transcript_text(transcript_len),
        "summary": f"### Topic\n- discussion {n}\n\n### Next Steps\n- (Ivan) task {n}",
        "participants": [
            {
                "name": "Owner Name",
                "origin": "self",
                "is_self": True,
                "emails": ["owner@example.com"],
                "spoke": True,
                "relationship": "self",
                "role": "self",
            },
            {
                "name": f"Guest {n}",
                "origin": "user",
                "is_self": False,
                "emails": [],
                "spoke": True,
                "relationship": "unknown",
                "role": "speaker-only",
            },
        ],
    }
    defaults.update(kw)
    return FakeMeeting(
        id=f"{n:08d}-0000-4000-8000-{n:012d}",
        title=title if title is not None else f"Synthetic meeting {n}",
        start=ts(start),
        modified_at=ts(start + timedelta(hours=1, minutes=30)),
        **defaults,
    )


__all__ = [
    "FOOTER",
    "HEADER",
    "FakeMeeting",
    "FakeWisprServer",
    "error_result",
    "make_meeting",
    "text_result",
    "ts",
]
