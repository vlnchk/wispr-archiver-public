"""Exit codes and the exception hierarchy.

Exception messages must never contain meeting content or secrets: they end up in logs
and alerts. Only ids, offsets, lengths, field names and statuses are allowed.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    PARTIAL = 1  # some meetings failed, run timeout, lock busy, generic error
    REAUTH = 2  # invalid_grant or 401 after refresh: human must run `login`
    FORMAT = 3  # Wispr response format changed: parser refused the data


#: Severity order when several problems happen in one run: 2 > 3 > 1 > 0.
_SEVERITY = {ExitCode.OK: 0, ExitCode.PARTIAL: 1, ExitCode.FORMAT: 2, ExitCode.REAUTH: 3}


def worst(*codes: ExitCode) -> ExitCode:
    return max(codes, key=lambda c: _SEVERITY[c], default=ExitCode.OK)


class ArchiverError(Exception):
    """Base class. `exit_code` tells the CLI how to terminate."""

    exit_code: ExitCode = ExitCode.PARTIAL
    kind: str = "error"


class ConfigError(ArchiverError):
    kind = "config"


class FormatError(ArchiverError):
    """The Wispr response does not match the observed contract. Never write such data."""

    exit_code = ExitCode.FORMAT
    kind = "format"


class ReauthRequired(ArchiverError):
    """Refresh token rejected or a 401 persists after refresh: `wispr-archiver login` needed."""

    exit_code = ExitCode.REAUTH
    kind = "reauth"


class LockBusy(ArchiverError):
    kind = "lock_busy"


class RunTimeout(ArchiverError):
    kind = "run_timeout"


class CircuitOpen(ArchiverError):
    """Too many consecutive transport failures: abort the run instead of hammering."""

    kind = "circuit_open"


class StoreError(ArchiverError):
    """Inconsistent archive on disk (e.g. snapshot exists with different bytes)."""

    kind = "store"


class PublishError(ArchiverError):
    """rclone could not be run (missing binary, timeout). `kind` names the cause."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class ToolError(ArchiverError):
    """An MCP tool call failed (isError=true or transport error).

    `kind` is one of: rate_limited, not_found, transient, account_disabled, tool_error.
    """

    def __init__(self, message: str, *, kind: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in {"rate_limited", "transient", "tool_error"}
