"""Test-wide setup: no network, hypothesis profile, anyio backend."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, settings

settings.register_profile(
    "default", deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=200
)
settings.register_profile("ci", parent=settings.get_profile("default"), max_examples=500)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

LIVE = os.environ.get("WISPR_LIVE") == "1"


class NetworkBlockedError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _isolate_config(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An installed worker or exported operator settings must not affect offline tests."""
    if request.node.get_closest_marker("live") and LIVE:
        return
    monkeypatch.setattr(
        "wispr_archiver.config.DEFAULT_CONFIG_PATH", tmp_path / "etc" / "worker.toml"
    )
    for key in tuple(os.environ):
        if key.startswith(("WISPR_ARCHIVER_", "RCLONE_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("WISPR_ARCHIVER_TOKEN_PATH", str(tmp_path / "private" / "token.json"))


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Offline tests block external network connections. Unix sockets and loopback stay allowed."""
    if request.node.get_closest_marker("live") and LIVE:
        yield
        return
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0] if isinstance(address, tuple) else str(address)
            if host not in {"127.0.0.1", "::1", "localhost"}:
                raise NetworkBlockedError(f"network access blocked in tests: {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield


@pytest.fixture(autouse=True)
def _restore_umask() -> Iterator[None]:
    """The CLI tightens the process umask; keep that from leaking between tests."""
    old = os.umask(0o022)
    os.umask(old)
    yield
    os.umask(old)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if LIVE:
        return
    skip_live = pytest.mark.skip(reason="live test: set WISPR_LIVE=1 (owner only)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
