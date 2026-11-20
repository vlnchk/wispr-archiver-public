"""Read-only probe of the live response format (run by the operator).

The report contains STRUCTURE ONLY: key names, JSON types, string lengths, masked
timestamp formats, booleans and counts, marker/offset numbers, Wispr error messages.
Never titles, summaries, notes, transcripts, names or e-mails.
"""

from __future__ import annotations

import importlib.metadata
import itertools
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
from mcp import MCPError
from mcp.types import CallToolResult, TextContent

from wispr_archiver.errors import ArchiverError
from wispr_archiver.wispr.parse import _HEADER_RE, _MARKER_RE, assemble_full, decode_cursor
from wispr_archiver.wispr.transport import HttpObserver, McpToolCaller

PROBE_VERSION = 1
NONEXISTENT_ID = "00000000-0000-4000-8000-000000000000"
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_ASTRAL = re.compile(r"[\U00010000-\U0010FFFF]")
EXPECTED_PARAMS = {
    "search_meetings": {"limit", "cursor", "since", "until"},
    "get_meeting": {"meeting_id", "view_transcript", "view_content"},
    "get_meeting_participants_enriched": {"meeting_id"},
}


def mask(s: str) -> str:
    return re.sub(r"\d", "9", s)


def shape(value: Any, depth: int = 0) -> Any:
    """Content-free description of a JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, int | float):
        return {"number": value}
    if isinstance(value, str):
        out: dict[str, Any] = {"str_len": len(value)}
        if _TS.match(value):
            out["format"] = mask(value)
        return out
    if isinstance(value, list):
        if not value:
            return {"array_len": 0}
        if all(isinstance(v, dict) for v in value):
            keys: dict[str, set[str]] = {}
            for item in value:
                for k, v in item.items():
                    keys.setdefault(k, set()).add(type(v).__name__)
            return {
                "array_len": len(value),
                "item_keys": {k: sorted(t) for k, t in sorted(keys.items())},
            }
        return {"array_len": len(value), "item_types": sorted({type(v).__name__ for v in value})}
    if isinstance(value, dict):
        if depth >= 2:
            return {"object_keys": sorted(value)}
        return {k: shape(v, depth + 1) for k, v in value.items()}
    return {"type": type(value).__name__}


def chunk_info(raw: Any, *, wrapped: bool) -> dict[str, Any]:
    if raw is None:
        return {"value": "null"}
    if not isinstance(raw, str):
        return {"value": type(raw).__name__}
    info: dict[str, Any] = {"len": len(raw), "empty": raw == ""}
    body = raw
    m = _HEADER_RE.match(raw)
    info["header"] = m is not None
    if m:
        body = raw[m.end() :]
    info["footer_with_newline"] = body.endswith("\n<<<END TRANSCRIPT>>>")
    info["footer_bare"] = body == "<<<END TRANSCRIPT>>>"
    if wrapped and info["footer_with_newline"]:
        body = body[: -len("\n<<<END TRANSCRIPT>>>")]
    mm = _MARKER_RE.search(body)
    info["marker"] = (
        {
            "remaining": int(mm.group(1)),
            "view": mm.group(2),
            "next_start": int(mm.group(3)),
            "text_len": mm.start(),
        }
        if mm
        else None
    )
    info["has_astral_chars"] = bool(_ASTRAL.search(body))
    info["starts_with_angle_brackets"] = raw.startswith("<<<")
    return info


class Probe:
    def __init__(
        self, caller: McpToolCaller, observer: HttpObserver, *, pause: float = 1.0
    ) -> None:
        self.caller = caller
        self.observer = observer
        self.pause = pause
        self.statuses: Counter[int] = Counter()
        self.retry_after: list[float] = []
        self.report: dict[str, Any] = {
            "probe_version": PROBE_VERSION,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "mcp_sdk": importlib.metadata.version("mcp"),
            "errors": [],
        }

    async def call(
        self, name: str, args: dict[str, Any]
    ) -> tuple[CallToolResult | None, dict[str, Any]]:
        await anyio.sleep(self.pause)  # <= 1 request/second
        meta: dict[str, Any] = {}
        try:
            result = await self.caller.call_tool(name, args)
        except ArchiverError as exc:
            meta["exception"] = {"class": type(exc).__name__, "kind": exc.kind, "message": str(exc)}
            result = None
        except MCPError as exc:  # pragma: no cover - the adapter maps these
            meta["exception"] = {"class": "MCPError", "code": exc.code}
            result = None
        if self.observer.status is not None:
            self.statuses[self.observer.status] += 1
            meta["http_status"] = self.observer.status
        if self.observer.retry_after is not None:
            self.retry_after.append(self.observer.retry_after)
        if result is not None:
            texts = [c.text for c in result.content if isinstance(c, TextContent)]
            meta["is_error"] = result.is_error
            meta["content_types"] = [c.type for c in result.content]
            meta["structured_content"] = (
                shape(result.structured_content) if result.structured_content is not None else None
            )
            if result.is_error:
                meta["error_text"] = "\n".join(texts)[:300]
            else:
                try:
                    payload = json.loads(texts[0]) if texts else None
                    meta["text_is_json_object"] = isinstance(payload, dict)
                except json.JSONDecodeError:
                    meta["text_is_json_object"] = False
        return result, meta

    @staticmethod
    def payload(result: CallToolResult | None) -> dict[str, Any] | None:
        if result is None or result.is_error:
            return None
        for c in result.content:
            if isinstance(c, TextContent):
                try:
                    data = json.loads(c.text)
                except json.JSONDecodeError:
                    return None
                return data if isinstance(data, dict) else None
        return None

    async def section(self, name: str, fn: Callable[[], Awaitable[Any]]) -> None:
        try:
            self.report[name] = await fn()
        except Exception as exc:  # the probe must finish and report what it saw
            detail = str(exc) if isinstance(exc, ArchiverError) else ""
            self.report["errors"].append(
                {"section": name, "class": type(exc).__name__, "message": detail[:300]}
            )

    # --- sections ---------------------------------------------------------------------

    async def tools(self) -> Any:
        listed = await self.caller.list_tools()
        out = []
        for t in listed.tools:
            props = (
                (t.input_schema or {}).get("properties", {})
                if isinstance(t.input_schema, dict)
                else {}
            )
            entry: dict[str, Any] = {
                "name": t.name,
                "input_properties": {
                    k: v.get("type") if isinstance(v, dict) else None for k, v in props.items()
                },
                "required": (t.input_schema or {}).get("required")
                if isinstance(t.input_schema, dict)
                else None,
                "has_output_schema": getattr(t, "output_schema", None) is not None,
            }
            if t.name in EXPECTED_PARAMS:
                entry["missing_expected_params"] = sorted(EXPECTED_PARAMS[t.name] - set(props))
            out.append(entry)
        return out

    async def run(self) -> dict[str, Any]:
        await self.section("tools", self.tools)

        listed: list[dict[str, Any]] = []

        async def search() -> Any:
            r, meta = await self.call("search_meetings", {"limit": 200})
            p = self.payload(r) or {}
            meetings = p.get("meetings") or []
            listed.extend(m for m in meetings if isinstance(m, dict))
            mods = [m.get("modified_at") for m in listed]
            cursor = p.get("next_cursor")
            dec = decode_cursor(cursor) if isinstance(cursor, str) else None
            return {
                "result": meta,
                "payload_shape": shape({k: v for k, v in p.items() if k != "meetings"}),
                "meeting_keys": shape(meetings).get("item_keys") if meetings else None,
                "sorted_by_modified_desc": all(
                    a >= b for a, b in itertools.pairwise(mods) if a and b
                ),
                "cursor_format": None
                if dec is None
                else {"modified_at": mask(dec.modified_at), "offset": dec.offset},
                "stats": {
                    "listed": len(listed),
                    "finalized_false": sum(1 for m in listed if m.get("finalized") is False),
                    "has_transcript_false": sum(
                        1 for m in listed if m.get("has_transcript") is False
                    ),
                    "shared_true": sum(1 for m in listed if m.get("shared")),
                    "with_folders": sum(1 for m in listed if m.get("folders")),
                    "with_attendees": sum(1 for m in listed if m.get("attendees")),
                    "start_null": sum(1 for m in listed if not m.get("start")),
                },
            }

        await self.section("search_meetings", search)
        finalized = [m for m in listed if m.get("finalized") and m.get("has_transcript")]

        async def transcript_first_chunks() -> Any:
            out = []
            for m in finalized[:5]:
                r, meta = await self.call(
                    "get_meeting",
                    {
                        "meeting_id": m["id"],
                        "view_transcript": {"start_char": 0, "char_limit": 40000},
                    },
                )
                p = self.payload(r) or {}
                out.append(
                    {
                        "meeting": m["id"],
                        "result": meta,
                        "payload_keys": shape(
                            {
                                k: v
                                for k, v in p.items()
                                if k not in ("transcript", "content", "summary", "title")
                            }
                        ),
                        "summary_len": len(p.get("summary") or ""),
                        "transcript": chunk_info(p.get("transcript"), wrapped=True),
                        "content_key_present": "content" in p,
                        "content_len_without_view": len(p["content"])
                        if isinstance(p.get("content"), str)
                        else None,
                    }
                )
            return out

        await self.section("get_meeting_transcript", transcript_first_chunks)

        async def assembly() -> Any:
            out = []
            sample = self.report.get("get_meeting_transcript") or []
            chosen = [
                s["meeting"]
                for s in sample
                if (s["transcript"].get("marker") and s["transcript"].get("has_astral_chars"))
            ]
            chosen += [
                s["meeting"]
                for s in sample
                if s["transcript"].get("marker") and s["meeting"] not in chosen
            ]
            for mid in chosen[:3]:

                async def fetch(start: int, limit: int, mid: str = mid) -> str | None:
                    r, _ = await self.call(
                        "get_meeting",
                        {
                            "meeting_id": mid,
                            "view_transcript": {"start_char": start, "char_limit": limit},
                        },
                    )
                    p = self.payload(r) or {}
                    t = p.get("transcript")
                    return t if isinstance(t, str) else None

                try:
                    got = await assemble_full(
                        fetch, view="view_transcript", wrapped=True, limit=40000
                    )
                    out.append(
                        {
                            "meeting": mid,
                            "ok": True,
                            "calls": got.calls,
                            "chars": len(got.text),
                            "char_unit": got.char_unit,
                            "has_astral_chars": bool(_ASTRAL.search(got.text)),
                        }
                    )
                except ArchiverError as exc:
                    out.append({"meeting": mid, "ok": False, "error": str(exc)[:300]})
            return out

        await self.section("full_transcript_assembly", assembly)

        async def notes() -> Any:
            out = []
            for m in [m for m in listed if m.get("finalized")][:10]:
                r, meta = await self.call(
                    "get_meeting",
                    {"meeting_id": m["id"], "view_content": {"start_char": 0, "char_limit": 200}},
                )
                p = self.payload(r) or {}
                info = chunk_info(p.get("content"), wrapped=False)
                out.append({"meeting": m["id"], "result": meta, "content": info})
                if info.get("marker"):
                    break
            return out

        await self.section("view_content", notes)

        async def no_transcript() -> Any:
            target = next((m for m in listed if m.get("has_transcript") is False), None)
            if target is None:
                return {"note": "no meeting with has_transcript=false"}
            r, meta = await self.call(
                "get_meeting",
                {
                    "meeting_id": target["id"],
                    "view_transcript": {"start_char": 0, "char_limit": 1000},
                },
            )
            p = self.payload(r) or {}
            return {
                "meeting": target["id"],
                "result": meta,
                "transcript_key_present": "transcript" in p,
                "transcript": chunk_info(p.get("transcript"), wrapped=True),
            }

        await self.section("has_transcript_false", no_transcript)

        async def not_found() -> Any:
            _, meta = await self.call("get_meeting", {"meeting_id": NONEXISTENT_ID})
            return meta

        await self.section("not_found", not_found)

        async def participants() -> Any:
            target = next((m for m in finalized if not m.get("shared")), None)
            if target is None:
                return {"note": "no own finalized meeting"}
            r, meta = await self.call(
                "get_meeting_participants_enriched", {"meeting_id": target["id"]}
            )
            p = self.payload(r) or {}
            return {
                "meeting": target["id"],
                "result": meta,
                "payload_shape": shape(
                    {k: v for k, v in p.items() if k not in ("participants", "user")}
                ),
                "participant_keys": shape(p.get("participants") or []).get("item_keys")
                if p.get("participants")
                else None,
                "user_keys": sorted(p["user"]) if isinstance(p.get("user"), dict) else None,
            }

        await self.section("participants", participants)

        async def windows() -> Any:
            starts = sorted(str(m["start"]) for m in listed if m.get("start"))
            if not starts:
                return {"note": "no start timestamps"}
            mid = datetime.fromisoformat(starts[len(starts) // 2])
            since = mid.isoformat()
            until = (mid + timedelta(microseconds=1)).isoformat()
            out: dict[str, Any] = {"pivot_is_a_meeting_start": True}
            for label, args in (
                ("since_only", {"since": since}),
                ("until_only", {"until": since}),
                ("until_plus_1us", {"until": until}),
            ):
                r, meta = await self.call("search_meetings", {"limit": 200, **args})
                p = self.payload(r) or {}
                out[label] = {
                    "count": len(p.get("meetings") or []),
                    "truncated": p.get("truncated"),
                    "is_error": meta.get("is_error"),
                }
            out["listed_total"] = len(listed)
            return out

        await self.section("since_until", windows)
        self.report["http_statuses_seen"] = dict(self.statuses)
        self.report["retry_after_seen"] = self.retry_after
        return self.report
