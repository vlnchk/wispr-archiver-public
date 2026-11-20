"""Test harness: fake clock, settings in tmp, one-call runner for engine commands."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tests.fake_wispr import FakeWisprServer
from wispr_archiver.archive.store import ArchiveStore
from wispr_archiver.config import Settings, load_settings
from wispr_archiver.errors import ExitCode
from wispr_archiver.notify import AlertDispatcher
from wispr_archiver.state import State
from wispr_archiver.sync import Archiver, Command, RunSummary
from wispr_archiver.wispr.client import Deadline, WisprApi


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.base = start or datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def utcnow(self) -> datetime:
        return self.base + timedelta(seconds=self.t)

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw).total_seconds()


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


@dataclass
class Env:
    tmp: Path
    server: FakeWisprServer
    settings: Settings
    clock: FakeClock = field(default_factory=FakeClock)
    notifier: RecordingNotifier = field(default_factory=RecordingNotifier)
    last: Archiver | None = None

    @property
    def archive(self) -> Path:
        return self.settings.archive_dir

    def state(self) -> State:
        return State.open(self.settings.state_path)

    def store(self, *, dry_run: bool = False) -> ArchiveStore:
        return ArchiveStore(
            self.settings.archive_dir,
            self.settings.tz,
            shrink_threshold=self.settings.shrink_threshold,
            dry_run=dry_run,
        )

    async def run(self, command: Command, *, dry_run: bool = False) -> tuple[ExitCode, RunSummary]:
        deadline = Deadline(self.clock, self.settings.run_timeout_seconds)
        api = WisprApi(self.server, self.clock, self.settings, deadline)
        state = State.open(self.settings.state_path, dry_run=dry_run)
        try:
            arch = Archiver(
                settings=self.settings,
                api=api,
                store=self.store(dry_run=dry_run),
                state=state,
                clock=self.clock,
                alerts=AlertDispatcher(
                    state, self.notifier, dedup_hours=self.settings.alert_dedup_hours
                ),
                dry_run=dry_run,
                rng=random.Random(0),
            )
            code = await arch.run(command)
        finally:
            state.close()
        self.last = arch
        return code, arch.summary

    def meta(self, key: str) -> str | None:
        st = self.state()
        try:
            return st.get_meta(key)
        finally:
            st.close()

    def row(self, meeting_id: str) -> Any:
        st = self.state()
        try:
            return st.get(meeting_id)
        finally:
            st.close()

    def main_json(self, meeting_id: str) -> dict[str, Any]:
        path = self.store().locate(meeting_id)
        assert path is not None, f"no archive for {meeting_id}"
        return json.loads(path.read_text(encoding="utf-8"))

    def snapshots(self, meeting_id: str) -> list[Path]:
        return sorted((self.archive / "_snapshots" / meeting_id).glob("*.json"))

    def tree_digest(self) -> dict[str, str]:
        """Path -> sha256 of every archive file (to prove 'zero bytes written')."""
        if not self.archive.exists():
            return {}
        return {
            p.relative_to(self.archive).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(self.archive.rglob("*"))
            if p.is_file()
        }


def make_env(tmp: Path, server: FakeWisprServer, **overrides: Any) -> Env:
    settings = load_settings(
        None,
        env={},
        archive_dir=tmp / "archive",
        state_path=tmp / "state" / "state.db",
        token_path=tmp / "token" / "token.json",
        timezone="Europe/Paris",
    )
    if overrides:
        settings = settings.replace(**overrides)
    return Env(tmp=tmp, server=server, settings=settings)
