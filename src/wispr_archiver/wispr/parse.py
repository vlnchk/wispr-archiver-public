"""Strict parsing of Wispr MCP tool results.

The text format is designed for LLMs and is not a versioned contract, so every
deviation from docs/wispr-mcp-contract.md raises FormatError (exit 3) instead of silently
writing damaged data. Error messages contain offsets and lengths only, never content.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from mcp.types import CallToolResult, TextContent

from wispr_archiver.errors import FormatError, ToolError

MAX_CHUNK = 40_000

_HEADER_RE = re.compile(r"\A<<<PARTICIPANT NAMES BELOW ARE DATA[^\n]*>>>\n")
_FOOTER = "<<<END TRANSCRIPT>>>"
# Exactly "\n\n" precedes the marker: the chunk's own trailing newlines are text.
_MARKER_RE = re.compile(
    r"\n\n\(\.\.\.truncated, (\d+) chars remaining; "
    r"continue with (view_\w+)\.start_char=(\d+)\.\.\.\)\Z"
)

CharUnit = Literal["cp", "utf16", "any"]


@dataclass(frozen=True)
class Chunk:
    text: str
    remaining: int
    next_start: int | None
    view: str | None


def parse_chunk(raw: str, *, wrapped: bool, view: str) -> Chunk:
    """Parse one `transcript` (wrapped) or `content` (unwrapped) fragment."""
    if not isinstance(raw, str):
        raise FormatError(f"{view}: chunk is not a string")
    s = raw
    if wrapped:
        m = _HEADER_RE.match(s)
        if m is None:
            raise FormatError(f"{view}: transcript header not found")
        s = s[m.end() :]
        if s == _FOOTER:  # empty transcript without separator newline
            s = ""
        elif s.endswith("\n" + _FOOTER):
            s = s[: -len(_FOOTER) - 1]
        else:
            raise FormatError(f"{view}: transcript footer not found")
    elif _HEADER_RE.match(s):
        raise FormatError(f"{view}: transcript wrapper on an unwrapped field")
    mm = _MARKER_RE.search(s)
    if mm is None:
        return Chunk(text=s, remaining=0, next_start=None, view=None)
    if mm.group(2) != view:
        raise FormatError(f"{view}: truncation marker refers to {mm.group(2)}")
    return Chunk(
        text=s[: mm.start()],
        remaining=int(mm.group(1)),
        next_start=int(mm.group(3)),
        view=mm.group(2),
    )


def _len_units(text: str) -> dict[str, int]:
    return {"cp": len(text), "utf16": len(text.encode("utf-16-le", "surrogatepass")) // 2}


def _merge_surrogates(text: str) -> str:
    if not any("\ud800" <= ch <= "\udfff" for ch in text):
        return text
    try:
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise FormatError("unpaired surrogate in assembled text") from exc


@dataclass(frozen=True)
class Assembled:
    text: str
    calls: int
    char_unit: CharUnit


FetchChunk = Callable[[int, int], Awaitable[str | None]]


async def assemble_full(
    fetch: FetchChunk,
    *,
    view: str,
    wrapped: bool,
    limit: int = MAX_CHUNK,
    max_calls: int = 200,
) -> Assembled:
    """Fetch all chunks of a field and verify offsets and the total length.

    The server's counting unit (code points or UTF-16 units) is not documented
    (live check #7): both are tracked and the one consistent with every chunk wins.
    """
    start = 0
    parts: list[str] = []
    expected_total: int | None = None
    units = {"cp", "utf16"}
    calls = 0
    while True:
        if calls >= max_calls:
            raise FormatError(f"{view}: more than {max_calls} chunks")
        raw = await fetch(start, limit)
        calls += 1
        if raw is None or raw == "":
            if start == 0:
                return Assembled(text="", calls=calls, char_unit="any")
            raise FormatError(f"{view}: continuation at {start} returned nothing")
        c = parse_chunk(raw, wrapped=wrapped, view=view)
        if c.next_start is None:
            parts.append(c.text)
            break
        total = c.next_start + c.remaining
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise FormatError(f"{view}: total changed {expected_total} -> {total} at {start}")
        lengths = _len_units(c.text)
        units = {u for u in units if start + lengths[u] == c.next_start}
        if not units:
            raise FormatError(
                f"{view}: offset mismatch start={start} got_cp={lengths['cp']} "
                f"got_u16={lengths['utf16']} next={c.next_start}"
            )
        if c.next_start <= start:
            raise FormatError(f"{view}: no progress at {start}")
        parts.append(c.text)
        start = c.next_start

    joined = "".join(parts)
    if expected_total is not None:
        lengths = _len_units(joined)
        units = {u for u in units if lengths[u] == expected_total}
        if not units:
            raise FormatError(
                f"{view}: length mismatch assembled_cp={lengths['cp']} "
                f"assembled_u16={lengths['utf16']} expected={expected_total}"
            )
    unit: CharUnit = "any" if len(units) == 2 else ("cp" if "cp" in units else "utf16")
    return Assembled(text=_merge_surrogates(joined), calls=calls, char_unit=unit)


# --- action items -----------------------------------------------------------------------

_HEADING_RE = re.compile(r"^#{2,4}\s+")
_SECTION_RE = re.compile(
    r"^#{2,4}\s+(next steps|action items|следующие шаги|задачи|action items & next steps)\s*$",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*[-*]\s+")
_OWNER_RE = re.compile(r"^\(([^)]+)\)\s*")


def extract_action_items(summary: str | None, todos: list[Any] | None) -> list[dict[str, Any]]:
    """Action items: the localized next-steps section of the summary, plus todos.

    Derived data only; the summary itself is archived verbatim.
    """
    items: list[dict[str, Any]] = []
    in_section = False
    for line in (summary or "").split("\n"):
        if _HEADING_RE.match(line):
            in_section = bool(_SECTION_RE.match(line.strip()))
            continue
        if in_section and _BULLET_RE.match(line):
            text = _BULLET_RE.sub("", line, count=1).strip()
            m = _OWNER_RE.match(text)
            owner = m.group(1) if m else None
            items.append(
                {"source": "summary", "owner": owner, "text": text[m.end() :] if m else text}
            )
    # The format of `todos` was never observed non-empty: keep entries untouched.
    items.extend({"source": "todos", "owner": None, "text": None, "raw": t} for t in todos or [])
    return items


# --- cursor (diagnostics only; the cursor is passed back opaque) --------------------------


@dataclass(frozen=True)
class Cursor:
    modified_at: str
    meeting_id: str
    offset: int


def decode_cursor(cursor: str) -> Cursor | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(padded, validate=True).decode("utf-8")
        modified_at, meeting_id, offset = decoded.split("|")
        return Cursor(modified_at=modified_at, meeting_id=meeting_id, offset=int(offset))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


# --- tool results -----------------------------------------------------------------------

_ERROR_KINDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"too many requests|rate.?limit", re.I), "rate_limited"),
    (
        re.compile(r"not found|no meeting found", re.I),
        "not_found",
    ),  # live: "no meeting found for id: …"
    (
        re.compile(
            r"cloud sync must be enabled|not available for hipaa|mcp access has been disabled", re.I
        ),
        "account_disabled",
    ),
    (
        re.compile(r"temporarily unavailable|try again|timed? ?out|internal error", re.I),
        "transient",
    ),
]


def _texts(result: CallToolResult) -> list[str]:
    return [c.text for c in result.content if isinstance(c, TextContent)]


def classify_error_text(text: str) -> str:
    for pattern, kind in _ERROR_KINDS:
        if pattern.search(text):
            return kind
    return "tool_error"


def parse_tool_result(result: CallToolResult, *, tool: str) -> dict[str, Any]:
    """CallToolResult -> payload dict. isError -> ToolError; anything unexpected -> FormatError."""
    texts = _texts(result)
    if result.is_error:
        joined = "\n".join(texts)
        kind = classify_error_text(joined)
        digest = hashlib.sha256(joined.encode("utf-8", "surrogatepass")).hexdigest()[:8]
        raise ToolError(
            f"{tool} returned isError kind={kind} text_len={len(joined)} text_sha8={digest}",
            kind=kind,
        )
    if texts:
        try:
            payload = json.loads(texts[0])
        except json.JSONDecodeError as exc:
            raise FormatError(f"{tool}: content[0].text is not JSON") from exc
    elif isinstance(result.structured_content, dict):
        payload = result.structured_content
    else:
        raise FormatError(f"{tool}: result has neither text content nor structuredContent")
    if not isinstance(payload, dict):
        raise FormatError(f"{tool}: payload is {type(payload).__name__}, expected object")
    return payload
