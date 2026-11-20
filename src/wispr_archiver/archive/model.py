"""Canonical archive record (schema wispr-archive/v1).

`content_sha256` covers everything that came from Wispr (the "core"). Bookkeeping fields
(`OUT_OF_HASH`) can change without creating a new version.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from wispr_archiver import ARCHIVE_SCHEMA, ARCHIVE_SOURCE
from wispr_archiver.wispr.models import MeetingPayload, ParticipantsPayload
from wispr_archiver.wispr.parse import extract_action_items

OUT_OF_HASH = frozenset(
    {
        "content_sha256",
        "archived_at",
        "source",
        "archive_path",
        "deleted_upstream_at",
        "reappeared_at",
    }
)

_MEETING_KNOWN = frozenset(MeetingPayload.model_fields) | set(MeetingPayload.PROMOTED)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def core_of(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in OUT_OF_HASH}


def content_hash(record: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(core_of(record)))


def build_record(
    *,
    meeting: MeetingPayload,
    participants: ParticipantsPayload | None,
    transcript: str,
    notes: str,
    archived_at: str,
) -> dict[str, Any]:
    extra = meeting.extra
    participants_meta: dict[str, Any] | None = None
    if participants is not None:
        participants_meta = {
            "user": participants.user,
            "mentioned_contacts": participants.mentioned_contacts,
            "note": participants.note,
            **participants.extra,
        }
    core: dict[str, Any] = {
        "schema": ARCHIVE_SCHEMA,
        "id": meeting.id,
        "title": meeting.title,
        "start": meeting.start,
        "end": meeting.end,
        "modified_at": meeting.modified_at,
        "finalized": meeting.finalized,
        "has_transcript": meeting.has_transcript,
        "share_link": meeting.share_link,
        "shared": extra.get("shared"),
        "owner": extra.get("owner"),
        "folders": extra.get("folders"),
        "attendees_calendar": meeting.attendees,
        "participants": participants.participants if participants is not None else [],
        "participants_meta": participants_meta,
        "summary_md": meeting.summary or "",
        "notes_md": notes,
        "action_items": extract_action_items(meeting.summary, meeting.todos),
        "todos_raw": meeting.todos,
        "transcript_chars": len(transcript),
        "transcript_text": transcript,
        "upstream_extra": {k: v for k, v in extra.items() if k not in _MEETING_KNOWN},
    }
    return {
        **core,
        "content_sha256": sha256_hex(canonical_json(core)),
        "archived_at": archived_at,
        "source": ARCHIVE_SOURCE,
        "archive_path": None,
        "deleted_upstream_at": None,
        "reappeared_at": None,
    }


def record_json(record: dict[str, Any]) -> str:
    """Stable on-disk JSON (pretty, UTF-8, key order as built)."""
    return json.dumps(record, ensure_ascii=False, indent=2) + "\n"
