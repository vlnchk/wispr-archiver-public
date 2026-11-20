"""JSON-lines logging to stdout (journald) with a field whitelist.

Only ids, lengths, hashes, statuses, counters and timings may be logged.
Any other field passed to `event()` is dropped and its *name* is reported in
`dropped_fields`, so a mistake is visible but nothing leaks. Records from third-party
loggers (mcp, httpx2, ...) are limited to WARNING, truncated and scrubbed of
token-looking strings.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

LOGGER_NAME = "wispr_archiver"

ALLOWED_FIELDS = frozenset(
    {
        # identity / context
        "run_id",
        "command",
        "meeting_id",
        "id8",
        "tool",
        "dry_run",
        "unit",
        # statuses
        "status",
        "reason",
        "prev_status",
        "exit_code",
        "error_class",
        "error_kind",
        "error",
        "alert",
        "suppressed",
        # versions / hashes / times
        "modified_at",
        "prev_modified_at",
        "content_sha256",
        "prev_sha256",
        "watermark",
        "new_watermark",
        "expires_at",
        "expires_in",
        "token_sha8",
        "rotated",
        # lengths / offsets
        "transcript_chars",
        "prev_transcript_chars",
        "notes_chars",
        "summary_chars",
        "participants",
        "action_items",
        "chunk_start",
        "chunk_len",
        "remaining",
        "char_unit",
        "bytes",
        "bytes_written",
        "calls",
        "count",
        "page",
        "has_more",
        "truncated",
        "cursor_present",
        "window_since",
        "window_until",
        "files",
        "fraction",
        # counters
        "attempt",
        "attempts",
        "listed",
        "new",
        "updated",
        "unchanged",
        "skipped",
        "guarded",
        "failed",
        "pending",
        "snapshots",
        "deleted_marked",
        "reappeared",
        "checked",
        "mismatches",
        "retries",
        "blocked",
        "not_found",
        "format_errors",
        "problems",
        "candidates",
        # publish / verify-remote (rclone counters only, never its messages)
        "step",
        "rclone_exit",
        "transfers",
        "checks",
        "errors",
        "deletes",
        "matched",
        "missing",
        "differ",
        "local_files",
        # timings
        "duration_ms",
        "sleep_s",
        "retry_after_s",
        "elapsed_s",
        "age_h",
    }
)

_TOKENISH = re.compile(r"[A-Za-z0-9_\-+/=.]{24,}")
_LIB_MSG_MAX = 300


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
        }
        fields: dict[str, Any] | None = getattr(record, "fields", None)
        if fields is not None and record.name.startswith(LOGGER_NAME):
            out["event"] = record.getMessage()
            dropped = []
            for key, value in fields.items():
                if key in ALLOWED_FIELDS:
                    out[key] = value
                else:
                    dropped.append(key)
            if dropped:
                out["dropped_fields"] = sorted(dropped)
        else:
            # Third-party or plain logging call: keep it short and scrub token-like strings.
            message = _TOKENISH.sub("[redacted]", record.getMessage())[:_LIB_MSG_MAX]
            out["event"] = "lib_log"
            out["logger"] = record.name
            out["msg"] = message
            if record.exc_info and record.exc_info[0] is not None:
                out["error_class"] = record.exc_info[0].__name__
        return json.dumps(out, ensure_ascii=False, default=str, sort_keys=False)


_logger = logging.getLogger(LOGGER_NAME)


def event(name: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log one structured event. Field names outside ALLOWED_FIELDS are dropped."""
    _logger.log(level, name, extra={"fields": fields})


def warn(name: str, **fields: Any) -> None:
    event(name, logging.WARNING, **fields)


def error(name: str, **fields: Any) -> None:
    event(name, logging.ERROR, **fields)


def setup_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    _logger.setLevel(level.upper())
    for lib in ("mcp", "mcp_types", "httpx", "httpx2", "httpcore", "anyio", "uvicorn"):
        logging.getLogger(lib).setLevel(logging.WARNING)
