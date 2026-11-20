"""Parser examples and property tests for the observed Wispr format."""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcp.types import CallToolResult, TextContent

from tests.wispr_format import FOOTER, HEADER, chunk, marker, transcript_text, ulen
from wispr_archiver.errors import FormatError, ToolError
from wispr_archiver.wispr.parse import (
    assemble_full,
    decode_cursor,
    extract_action_items,
    parse_chunk,
    parse_tool_result,
)

pytestmark = pytest.mark.anyio


class Server:
    """Serves chunks of `full` like get_meeting does; counts calls."""

    def __init__(
        self, full: str, *, wrapped: bool = True, view: str = "view_transcript", unit: str = "cp"
    ) -> None:
        self.full = full
        self.wrapped = wrapped
        self.view = view
        self.unit = unit
        self.calls = 0

    async def fetch(self, start: int, limit: int) -> str:
        self.calls += 1
        return chunk(self.full, start, limit, view=self.view, wrapped=self.wrapped, unit=self.unit)  # type: ignore[arg-type]


# --- group 1/2: long transcript, 40 000 and 1 200 char chunks -----------------------------


async def test_long_transcript_two_calls() -> None:
    full = transcript_text(50_000)
    s = Server(full)
    got = await assemble_full(s.fetch, view="view_transcript", wrapped=True, limit=40_000)
    assert got.text == full
    assert s.calls == 2


async def test_small_chunks() -> None:
    full = transcript_text(50_000)
    s = Server(full)
    got = await assemble_full(s.fetch, view="view_transcript", wrapped=True, limit=1_200)
    assert got.text == full
    assert s.calls == -(-50_000 // 1_200)


# --- group 3/4: exact marker, format drift ------------------------------------------------


def test_literal_continuation_marker() -> None:
    # A synthetic example of the marker syntax (written out on purpose, not via the helper).
    raw = (
        HEADER
        + "abc\n\n(...truncated, 49997 chars remaining; "
        + "continue with view_transcript.start_char=3...)"
        + FOOTER
    )
    c = parse_chunk(raw, wrapped=True, view="view_transcript")
    assert (c.text, c.remaining, c.next_start) == ("abc", 49_997, 3)


@pytest.mark.parametrize(
    "raw",
    [
        "no header here",
        HEADER + "text without footer",
        "<<<SOMETHING ELSE>>>\ntext" + FOOTER,
        HEADER + "abc" + marker(5, "view_content", 3) + FOOTER,  # marker for the wrong view
    ],
)
def test_format_drift_raises(raw: str) -> None:
    with pytest.raises(FormatError):
        parse_chunk(raw, wrapped=True, view="view_transcript")


def test_trailing_newlines_belong_to_text() -> None:
    # Exactly "\n\n" precedes the marker; the chunk's own trailing "\n" is text.
    raw = HEADER + "line\n" + marker(1, "view_transcript", 5) + FOOTER
    c = parse_chunk(raw, wrapped=True, view="view_transcript")
    assert c.text == "line\n"
    assert c.next_start == 5


def test_empty_wrapped_transcript_variants() -> None:
    assert parse_chunk(HEADER + FOOTER, wrapped=True, view="view_transcript").text == ""
    assert (
        parse_chunk(HEADER + "<<<END TRANSCRIPT>>>", wrapped=True, view="view_transcript").text
        == ""
    )


async def test_no_transcript_returns_empty() -> None:
    async def none(start: int, limit: int) -> str | None:
        return None

    got = await assemble_full(none, view="view_transcript", wrapped=True, limit=40_000)
    assert got.text == ""
    assert got.calls == 1


async def test_offset_mismatch_detected() -> None:
    async def bad(start: int, limit: int) -> str:
        return HEADER + "abc" + marker(10, "view_transcript", 7) + FOOTER

    with pytest.raises(FormatError, match="offset"):
        await assemble_full(bad, view="view_transcript", wrapped=True, limit=40_000)


async def test_total_length_mismatch_detected() -> None:
    calls = {"n": 0}

    async def short(start: int, limit: int) -> str:
        calls["n"] += 1
        if start == 0:
            return HEADER + "abc" + marker(10, "view_transcript", 3) + FOOTER
        return HEADER + "def" + FOOTER  # 7 chars missing

    with pytest.raises(FormatError, match="length"):
        await assemble_full(short, view="view_transcript", wrapped=True, limit=40_000)


async def test_missing_continuation_detected() -> None:
    async def gone(start: int, limit: int) -> str | None:
        if start == 0:
            return HEADER + "abc" + marker(3, "view_transcript", 3) + FOOTER
        return None

    with pytest.raises(FormatError):
        await assemble_full(gone, view="view_transcript", wrapped=True, limit=40_000)


async def test_no_progress_detected() -> None:
    async def stuck(start: int, limit: int) -> str:
        return HEADER + marker(3, "view_transcript", 0) + FOOTER

    with pytest.raises(FormatError):
        await assemble_full(stuck, view="view_transcript", wrapped=True, limit=40_000)


async def test_content_unwrapped_with_marker() -> None:
    full = "# Notes\n" + "- item\n" * 50
    s = Server(full, wrapped=False, view="view_content")
    got = await assemble_full(s.fetch, view="view_content", wrapped=False, limit=64)
    assert got.text == full


async def test_notes_starting_with_angle_brackets_are_fine() -> None:
    async def notes(start: int, limit: int) -> str:
        return "<<< my notes >>>"

    got = await assemble_full(notes, view="view_content", wrapped=False, limit=40_000)
    assert got.text == "<<< my notes >>>"


async def test_content_that_looks_wrapped_is_rejected() -> None:
    async def wrapped(start: int, limit: int) -> str:
        return HEADER + "notes" + FOOTER

    with pytest.raises(FormatError):
        await assemble_full(wrapped, view="view_content", wrapped=False, limit=40_000)


# --- char units: code points vs UTF-16 ([?] live check #7) ---------------------------------


@pytest.mark.parametrize("unit", ["cp", "utf16"])
async def test_emoji_in_both_units(unit: str) -> None:
    full = "Speaker A:  hello 😀 world 🎉\n" * 40
    s = Server(full, unit=unit)
    got = await assemble_full(s.fetch, view="view_transcript", wrapped=True, limit=7)
    assert got.text == full
    assert got.char_unit == unit


# --- group 5: cursor ----------------------------------------------------------------------


def test_decode_cursor() -> None:
    cur = decode_cursor(
        "MjAwMS0wMi0wM1QwOTo0MDowMC4wMDAwMDB8MDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAyfDU"
    )
    assert cur.offset == 5
    assert cur.modified_at.startswith("2001-02-03T09:40:00")
    assert cur.meeting_id == "00000000-0000-4000-8000-000000000002"


def test_decode_cursor_garbage_is_none() -> None:
    assert decode_cursor("!!!not-base64") is None


# --- group 6: action items ----------------------------------------------------------------


def test_action_items_ru_en() -> None:
    sum_ru = "### Topic\n- item\n\n### Следующие шаги\n- (Ivan) Send the report\n- Unassigned"
    ai = extract_action_items(sum_ru, [])
    assert [(a["owner"], a["text"]) for a in ai] == [
        ("Ivan", "Send the report"),
        (None, "Unassigned"),
    ]
    assert len(extract_action_items("### Next Steps\n- (Anna) Send report", [])) == 1
    assert extract_action_items(None, None) == []


def test_action_items_section_ends_at_next_heading() -> None:
    s = "## Action Items\n* (A) one\n## Other\n- not an item"
    assert [a["text"] for a in extract_action_items(s, [])] == ["one"]


def test_todos_are_kept_raw() -> None:
    items = extract_action_items("", [{"title": "x", "done": False}, "plain"])
    assert items == [
        {"source": "todos", "owner": None, "text": None, "raw": {"title": "x", "done": False}},
        {"source": "todos", "owner": None, "text": None, "raw": "plain"},
    ]


# --- group 8: tool results ----------------------------------------------------------------


def result(text: str, *, is_error: bool = False, structured: Any = None) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        is_error=is_error,
        structured_content=structured,
    )


def test_parse_tool_result_json_text() -> None:
    assert parse_tool_result(result('{"a":1}'), tool="t") == {"a": 1}


def test_parse_tool_result_structured_only() -> None:
    r = CallToolResult(content=[], structured_content={"b": 2})
    assert parse_tool_result(r, tool="t") == {"b": 2}


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Too many requests", "rate_limited"),
        ("Meeting or calendar event not found", "not_found"),
        ("no meeting found for id: 00000000-0000-4000-8000-000000000000", "not_found"),  # live
        ("Cloud Sync must be enabled to use this feature.", "account_disabled"),
        ("This feature is not available for HIPAA-covered accounts.", "account_disabled"),
        ("MCP access has been disabled by your organization's administrator.", "account_disabled"),
        ("Service temporarily unavailable", "transient"),
        ("Something unexpected", "tool_error"),
    ],
)
def test_parse_tool_result_errors(text: str, kind: str) -> None:
    with pytest.raises(ToolError) as exc:
        parse_tool_result(result(text, is_error=True), tool="get_meeting")
    assert exc.value.kind == kind
    assert text not in str(exc.value)  # server text is not echoed into logs


@pytest.mark.parametrize("text", ["not json", "[1,2]", '"str"'])
def test_parse_tool_result_non_object_is_format_error(text: str) -> None:
    with pytest.raises(FormatError):
        parse_tool_result(result(text), tool="t")


def test_parse_tool_result_empty_is_format_error() -> None:
    with pytest.raises(FormatError):
        parse_tool_result(CallToolResult(content=[]), tool="t")


# --- property tests -----------------------------------------------------------------------

texts = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=0, max_size=400)


@given(
    full=texts, limit=st.integers(min_value=1, max_value=97), unit=st.sampled_from(["cp", "utf16"])
)
async def test_property_roundtrip_transcript(full: str, limit: int, unit: str) -> None:
    s = Server(full, unit=unit)
    got = await assemble_full(
        s.fetch, view="view_transcript", wrapped=True, limit=limit, max_calls=10_000
    )
    assert got.text == full
    assert s.calls == max(1, -(-ulen(full, unit) // limit))  # type: ignore[arg-type]


@given(full=texts, limit=st.integers(1, 97))
async def test_property_roundtrip_content(full: str, limit: int) -> None:
    s = Server(full, wrapped=False, view="view_content")
    got = await assemble_full(
        s.fetch, view="view_content", wrapped=False, limit=limit, max_calls=10_000
    )
    assert got.text == full


def test_cursor_payload_is_json_safe() -> None:
    # decode_cursor must never raise on arbitrary input (diagnostics only)
    for s in ["", "a", "Zm9v", json.dumps({"x": 1})]:
        decode_cursor(s)
