"""Derived Markdown with YAML frontmatter. The JSON record is the source of truth."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from wispr_archiver.wispr.models import parse_ts


def _q(value: Any) -> str:
    """YAML scalar via JSON (a JSON string is a valid YAML double-quoted scalar)."""
    return json.dumps(value, ensure_ascii=False)


def _local(ts: str | None, tz: ZoneInfo) -> str:
    if not ts:
        return ""
    try:
        dt: datetime = parse_ts(ts).astimezone(tz)
    except ValueError:
        return ""
    return f"{dt:%Y-%m-%d %H:%M} ({tz.key})"


def participant_labels(record: dict[str, Any]) -> list[str]:
    labels = []
    for p in record.get("participants") or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "?")
        emails = p.get("emails") or []
        labels.append(f"{name} <{emails[0]}>" if emails else name)
    return labels


def _action_item(item: dict[str, Any]) -> str:
    text = item.get("text")
    if text is None:
        raw = item.get("raw")
        if isinstance(raw, dict):
            text = raw.get("text") or raw.get("title")
        if text is None:
            text = json.dumps(raw, ensure_ascii=False)
    owner = item.get("owner")
    return f"- [ ] {f'**{owner}**: ' if owner else ''}{text}"


@dataclass(frozen=True)
class MarkdownLabels:
    untitled: str = "Untitled"
    removed: str = "Meeting removed from Wispr (detected {at}). This is the archived copy."
    start: str = "Start"
    end: str = "End"
    participants: str = "Participants"
    none: str = "_None_"
    notes: str = "My notes"
    transcript: str = "Transcript"
    empty: str = "_No transcript available_"


# Read compatibility for archives produced before the English release.
LEGACY_LABELS = MarkdownLabels(
    untitled="Без названия",
    removed="Встреча удалена в Wispr (обнаружено {at}). Это архивная копия.",
    start="Начало",
    end="Конец",
    participants="Участники",
    none="_нет_",
    notes="Мои заметки",
    transcript="Транскрипт",
    empty="_транскрипт отсутствует_",
)


def render_markdown(
    record: dict[str, Any], tz: ZoneInfo, labels: MarkdownLabels = MarkdownLabels()
) -> str:
    people = participant_labels(record)
    front = [
        "---",
        f"schema: {_q(record['schema'])}",
        f"wispr_id: {_q(record['id'])}",
        f"title: {_q(record.get('title') or '')}",
        f"start: {_q(record.get('start'))}",
        f"end: {_q(record.get('end'))}",
        f"start_local: {_q(_local(record.get('start'), tz))}",
        f"modified_at: {_q(record['modified_at'])}",
        f"participants: {_q(people)}",
        f"share_link: {_q(record.get('share_link') or '')}",
        f"transcript_chars: {int(record['transcript_chars'])}",
        f"content_sha256: {_q(record['content_sha256'])}",
        f"archived_at: {_q(record['archived_at'])}",
    ]
    if record.get("deleted_upstream_at"):
        front.append(f"deleted_upstream_at: {_q(record['deleted_upstream_at'])}")
    if record.get("reappeared_at"):
        front.append(f"reappeared_at: {_q(record['reappeared_at'])}")
    front.append("---")

    body = ["", f"# {record.get('title') or labels.untitled}", ""]
    if record.get("deleted_upstream_at"):
        body += [
            "> " + labels.removed.format(at=record["deleted_upstream_at"]),
            "",
        ]
    body += [
        f"**{labels.start}:** {_local(record.get('start'), tz) or '—'} · "
        f"**{labels.end}:** {_local(record.get('end'), tz) or '—'}",
        "",
        f"**{labels.participants}:** {', '.join(people) if people else '—'}",
        "",
        "## Summary",
        "",
        record.get("summary_md") or labels.none,
        "",
        "## Action items",
        "",
        "\n".join(_action_item(a) for a in record.get("action_items") or []) or labels.none,
        "",
        f"## {labels.notes}",
        "",
        record.get("notes_md") or labels.none,
        "",
        f"## {labels.transcript}",
        "",
        record.get("transcript_text") or labels.empty,
        "",
    ]
    return "\n".join(front + body)
