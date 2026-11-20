"""Strict models of Wispr MCP payloads (docs/wispr-mcp-contract.md, Tools and result envelope).

Missing required keys or wrong types -> FormatError (exit 3). Unknown keys are kept
(`model_extra`) and archived in `upstream_extra`, so nothing the server sends is lost.
Keys that were always present in verified responses are required, even if nullable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from wispr_archiver.errors import FormatError


def parse_ts(value: str) -> datetime:
    """ISO 8601 with an offset (Wispr returns UTC with 'Z')."""
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError("timestamp without timezone")
    return ts


class _Payload(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    @property
    def extra(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


def _check_ts(value: str | None) -> str | None:
    if value is not None:
        parse_ts(value)
    return value


class ListedMeeting(_Payload):
    id: str
    title: str | None = None
    finalized: bool
    has_transcript: bool | None = None
    start: str | None
    end: str | None = None
    modified_at: str
    share_link: str | None = None
    attendees: list[Any] | None = None
    shared: bool | None = None

    @field_validator("start", "end", "modified_at")
    @classmethod
    def _timestamps(cls, v: str | None) -> str | None:
        return _check_ts(v)

    @field_validator("id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("empty id")
        return v


class SearchPage(_Payload):
    meetings: list[ListedMeeting]
    count: int | None = None
    has_more: bool
    next_cursor: str | None = None
    truncated: bool = False


class MeetingPayload(_Payload):
    id: str
    title: str | None
    summary: str | None = None  # absent for some meetings (live 2026-11-18)
    finalized: bool
    has_transcript: bool
    attendees: list[Any]
    todos: list[Any]
    start: str | None
    end: str | None
    modified_at: str
    share_link: str | None
    content: str | None = None
    transcript: str | None = None

    #: Extra keys promoted to top-level record fields (documented for shared/folder meetings).
    PROMOTED: ClassVar[tuple[str, ...]] = ("shared", "owner", "folders")

    @field_validator("start", "end", "modified_at")
    @classmethod
    def _timestamps(cls, v: str | None) -> str | None:
        return _check_ts(v)

    @property
    def is_shared(self) -> bool:
        return bool(self.extra.get("shared"))


class ParticipantsPayload(_Payload):
    meeting_id: str
    participants: list[dict[str, Any]]
    user: dict[str, Any] | None = None
    mentioned_contacts: list[Any] | None = None
    note: str | None = None


def validate[T: _Payload](model: type[T], payload: dict[str, Any], *, tool: str) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        # Only field locations and error types: never input values.
        where = sorted({".".join(str(p) for p in e["loc"]) + ":" + e["type"] for e in exc.errors()})
        raise FormatError(
            f"{tool}: payload does not match contract ({', '.join(where)[:300]})"
        ) from None
