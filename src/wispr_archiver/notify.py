"""Alerts with de-duplication.

Identical alerts (same kind + fingerprint) are suppressed for `alert_dedup_hours`, then
repeated as a reminder. The transport (Telegram, M5) sits behind the `Notifier` protocol.
Alert texts contain counts, ids and instructions only, never meeting content.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from wispr_archiver.config import Settings
from wispr_archiver.errors import ConfigError, ExitCode
from wispr_archiver.lock import run_lock
from wispr_archiver.log import event, warn
from wispr_archiver.state import State
from wispr_archiver.wispr.models import parse_ts

RUNBOOK = "docs/RUNBOOK.md"

REAUTH_TEXT = (
    "Wispr rejected the refresh token (exit 2). Sign in again. See " + RUNBOOK + "#sign-in-again."
)
FORMAT_TEXT = (
    "Wispr returned an unexpected response (exit 3). See " + RUNBOOK + "#response-format-changes. "
    "Details: {detail}"
)


@dataclass(frozen=True)
class Alert:
    kind: str
    text: str
    key: str = ""  # what makes two alerts "the same" (e.g. sorted failed ids)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.kind}|{self.key}".encode()).hexdigest()[:16]


class Notifier(Protocol):
    def send(self, text: str) -> bool | None: ...


class LogNotifier:
    """Fallback when no Telegram credentials are configured: the alert goes to the log."""

    def send(self, text: str) -> bool:
        warn("alert_not_delivered", reason="no_notifier_configured")
        return False


def make_notifier(s: Settings, *, dry_run: bool = False) -> Notifier:
    if dry_run or not (s.telegram_bot_token and s.telegram_chat_id):
        return LogNotifier()
    from wispr_archiver.telegram import TelegramNotifier  # HTTP stack only when needed

    return TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)


class AlertDispatcher:
    def __init__(self, state: State, notifier: Notifier, *, dedup_hours: float) -> None:
        self.state = state
        self.notifier = notifier
        self.window = timedelta(hours=dedup_hours)

    def dispatch(self, alert: Alert, *, now: datetime, run_id: str | None = None) -> bool:
        """True when this alert was delivered or a previous delivery covers it."""
        meta_key = f"alert:{alert.kind}"
        raw = self.state.get_meta(meta_key)
        last = json.loads(raw) if raw else {}
        suppressed = (
            last.get("fingerprint") == alert.fingerprint
            and now - parse_ts(last["at"]) < self.window
        )
        if not suppressed:
            if not _send(self.notifier, alert):
                return False
            self.state.set_meta(
                meta_key, json.dumps({"fingerprint": alert.fingerprint, "at": now.isoformat()})
            )
        if run_id:
            self.state.mark_run_alerted(run_id)
        event("alert", alert=alert.kind, suppressed=suppressed)
        return True


def _send(notifier: Notifier, alert: Alert) -> bool:
    try:
        return notifier.send(alert.text) is not False
    except Exception as exc:  # a transport error can contain the bot URL
        warn("alert_delivery_failed", alert=alert.kind, error_class=type(exc).__name__)
        return False


_MANAGED_JOBS = {
    f"wispr-archiver@{command}.service": command
    for command in ("sync", "publish", "reconcile", "deadman-check")
}


def notify_failure(
    s: Settings, unit: str, invocation: str, now: datetime, notifier: Notifier | None = None
) -> ExitCode:
    command = _MANAGED_JOBS.get(unit)
    if command is None:
        raise ConfigError("notify requires a managed service unit")
    notifier = notifier or make_notifier(s)
    alert = Alert(kind=f"systemd_{command}", text=f"{unit} failed. Check journalctl -u {unit}.")
    with ExitStack() as stack:
        try:
            stack.enter_context(run_lock(s.lock_path))
            state = stack.enter_context(closing(State.open(s.state_path)))
            alerted = state.run_alerted(invocation, command)
        except (OSError, sqlite3.Error) as exc:
            # The job may have failed before a run row existed, including on a broken disk.
            warn("notify_without_state", unit=unit, error_class=type(exc).__name__)
            return ExitCode.OK if _send(notifier, alert) else ExitCode.PARTIAL
        if alerted:
            event("alert", unit=unit, suppressed=True)
            return ExitCode.OK
        dispatcher = AlertDispatcher(state, notifier, dedup_hours=s.alert_dedup_hours)
        covered = dispatcher.dispatch(
            alert, now=now, run_id=invocation if alerted is False else None
        )
        return ExitCode.OK if covered else ExitCode.PARTIAL


def deadman(s: Settings, now: datetime, notifier: Notifier | None = None) -> ExitCode:
    with run_lock(s.lock_path), closing(State.open(s.state_path)) as state:
        last = state.get_meta("last_completed_at")
        reason = "never_completed"
        if last is not None:
            try:
                hours = (now - parse_ts(last)).total_seconds() / 3600
            except ValueError:
                hours = -1
            if 0 <= hours < s.deadman_hours:
                event("deadman_check", status="ok", age_h=round(hours, 2))
                return ExitCode.OK
            reason = "stale" if hours >= 0 else "invalid_timestamp"
        warn("deadman_check", status=reason)
        dispatcher = AlertDispatcher(
            state, notifier or make_notifier(s), dedup_hours=s.alert_dedup_hours
        )
        covered = dispatcher.dispatch(
            Alert(
                kind="deadman",
                key=last or reason,
                text=(
                    f"No archive run has completed within {s.deadman_hours:g} hours. "
                    "Check wispr-archiver-sync.timer and logs for wispr-archiver@sync.service."
                ),
            ),
            now=now,
        )
        return ExitCode.OK if covered else ExitCode.PARTIAL
