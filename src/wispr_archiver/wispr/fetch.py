"""Fetch one meeting completely: metadata, summary, full transcript, full notes, participants."""

from __future__ import annotations

from dataclasses import dataclass

from wispr_archiver.errors import FormatError, ToolError
from wispr_archiver.wispr.client import WisprApi
from wispr_archiver.wispr.models import MeetingPayload, ParticipantsPayload
from wispr_archiver.wispr.parse import CharUnit, assemble_full


@dataclass(frozen=True)
class FetchedMeeting:
    meeting: MeetingPayload
    transcript: str
    notes: str
    participants: ParticipantsPayload | None
    transcript_unit: CharUnit
    notes_unit: CharUnit


def _same_version(first: MeetingPayload, other: MeetingPayload) -> None:
    if other.modified_at != first.modified_at:
        # The meeting changed between chunk calls: the pieces may not fit. Retry later.
        raise ToolError("meeting changed during fetch", kind="transient")


async def fetch_meeting(
    api: WisprApi, meeting_id: str, *, chunk: int, shared_hint: bool = False
) -> FetchedMeeting | MeetingPayload:
    """Return FetchedMeeting, or the bare MeetingPayload if the meeting is not finalized."""
    first = await api.get_meeting(meeting_id, view_transcript=(0, chunk))
    if not first.finalized:
        return first
    if first.has_transcript and first.transcript is None:
        raise FormatError("get_meeting: has_transcript=true but no transcript field")

    async def transcript_chunk(start: int, limit: int) -> str | None:
        if start == 0:
            return first.transcript
        nxt = await api.get_meeting(meeting_id, view_transcript=(start, limit))
        _same_version(first, nxt)
        return nxt.transcript

    async def content_chunk(start: int, limit: int) -> str | None:
        nxt = await api.get_meeting(meeting_id, view_content=(start, limit))
        _same_version(first, nxt)
        return nxt.content

    transcript = await assemble_full(
        transcript_chunk, view="view_transcript", wrapped=True, limit=chunk
    )
    notes = await assemble_full(content_chunk, view="view_content", wrapped=False, limit=chunk)
    participants = None
    if not (shared_hint or first.is_shared):
        participants = await api.get_participants(meeting_id)
    return FetchedMeeting(
        meeting=first,
        transcript=transcript.text,
        notes=notes.text,
        participants=participants,
        transcript_unit=transcript.char_unit,
        notes_unit=notes.char_unit,
    )
