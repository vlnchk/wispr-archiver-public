"""Settings: defaults < TOML file < environment (WISPR_ARCHIVER_*) < CLI flags.

Secrets (Telegram bot token) are accepted only from the environment, never from TOML,
so the config file can be shown or copied without leaking anything.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wispr_archiver.errors import ConfigError

ENV_PREFIX = "WISPR_ARCHIVER_"
DEFAULT_CONFIG_PATH = Path("/etc/wispr-archiver/config.toml")
SECRET_FIELDS = frozenset({"telegram_bot_token"})

#: Hard limits from the observed contract / requirements; config may only tighten them.
MAX_CHUNK = 40_000
MAX_SEARCH_LIMIT = 200
MAX_REQUESTS_PER_SECOND = 1.0
MIN_RATE_LIMIT_PAUSE = 60.0


@dataclass(frozen=True)
class Settings:
    archive_dir: Path = Path("/var/lib/wispr-archiver/archive")
    state_path: Path = Path("/var/lib/wispr-archiver/state.db")
    token_path: Path = field(
        default_factory=lambda: Path.home() / ".config" / "wispr-archiver" / "token.json"
    )
    timezone: str = "UTC"
    login_port: int = 33418

    requests_per_second: float = 1.0
    chunk_size: int = MAX_CHUNK
    search_limit: int = MAX_SEARCH_LIMIT
    overlap_hours: float = 24.0
    shrink_threshold: float = 0.10
    max_attempts: int = 5
    rate_limit_retries: int = 3
    run_timeout_seconds: float = 1200.0
    rate_limit_pause_seconds: float = MIN_RATE_LIMIT_PAUSE
    circuit_breaker_failures: int = 8  # > max_attempts: one bad meeting must not abort the run
    refresh_margin_hours: float = 24.0

    reconcile_max_deleted_fraction: float = 0.20
    reconcile_block_min_count: int = 5
    alert_dedup_hours: float = 6.0
    deadman_hours: float = 3.0

    rclone_binary: str = "rclone"
    rclone_remote: str = "wispr-gdrive:Wispr Archive"
    rclone_config: Path | None = None
    publish_timeout_seconds: float = 1800.0

    telegram_chat_id: str | None = None
    telegram_bot_token: str | None = field(default=None, repr=False)

    @property
    def lock_path(self) -> Path:
        # Next to the token: the lock guards the single refresh-token holder,
        # whatever --state/--dest a command uses.
        return self.token_path.parent / "run.lock"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def replace(self, **changes: Any) -> Settings:
        return _validate(dataclasses.replace(self, **changes))


def _coerce(name: str, raw: Any, typ: Any) -> Any:
    text = str(raw)
    try:
        if typ is Path or typ == (Path | None):
            if typ != Path and text == "":
                return None
            return Path(text).expanduser()
        if typ is int:
            if isinstance(raw, bool):
                raise ValueError
            return int(raw)
        if typ is float:
            if isinstance(raw, bool):
                raise ValueError
            return float(raw)
        if typ is str or typ == (str | None):
            if not isinstance(raw, str | int):
                raise ValueError
            return text
    except ValueError as exc:
        raise ConfigError(f"invalid value for {name}") from exc
    raise ConfigError(f"unsupported type for {name}")  # pragma: no cover


def _remote_names_a_folder(remote: str) -> bool:
    """`name:folder` or an on-the-fly `:backend:folder`; never a bare remote root or a flag."""
    body = remote[1:] if remote.startswith(":") else remote
    name, sep, folder = body.partition(":")
    return bool(sep and name and not name.startswith("-") and folder.strip("/ "))


def _validate(s: Settings) -> Settings:
    checks: list[tuple[bool, str]] = [
        (
            0 < s.requests_per_second <= MAX_REQUESTS_PER_SECOND,
            "requests_per_second must be in (0, 1]",
        ),
        (0 < s.chunk_size <= MAX_CHUNK, f"chunk_size must be in (0, {MAX_CHUNK}]"),
        (
            0 < s.search_limit <= MAX_SEARCH_LIMIT,
            f"search_limit must be in (0, {MAX_SEARCH_LIMIT}]",
        ),
        (s.overlap_hours >= 0, "overlap_hours must be >= 0"),
        (0 <= s.shrink_threshold <= 0.1, "shrink_threshold must be in [0, 0.1]"),
        (s.max_attempts >= 1, "max_attempts must be >= 1"),
        (s.rate_limit_retries >= 1, "rate_limit_retries must be >= 1"),
        (s.run_timeout_seconds > 0, "run_timeout_seconds must be > 0"),
        (
            s.rate_limit_pause_seconds >= MIN_RATE_LIMIT_PAUSE,
            "rate_limit_pause_seconds must be >= 60",
        ),
        (s.circuit_breaker_failures >= 1, "circuit_breaker_failures must be >= 1"),
        (0 < s.deadman_hours < float("inf"), "deadman_hours must be finite and positive"),
        (s.refresh_margin_hours >= 0, "refresh_margin_hours must be >= 0"),
        (
            0 < s.reconcile_max_deleted_fraction <= 1,
            "reconcile_max_deleted_fraction must be in (0, 1]",
        ),
        (s.reconcile_block_min_count >= 1, "reconcile_block_min_count must be >= 1"),
        (1 <= s.login_port <= 65535, "login_port must be a TCP port"),
        (s.publish_timeout_seconds > 0, "publish_timeout_seconds must be > 0"),
        (
            _remote_names_a_folder(s.rclone_remote),
            "rclone_remote must be '<remote>:<folder>' with a non-empty folder",
        ),
    ]
    for ok, message in checks:
        if not ok:
            raise ConfigError(message)
    try:
        ZoneInfo(s.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError("unknown timezone") from exc
    return s


def load_settings(
    config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> Settings:
    """Build settings. `overrides` (CLI flags) win; None values are ignored."""
    env = os.environ if env is None else env
    hints = get_type_hints(Settings)
    names = {f.name for f in fields(Settings)}
    values: dict[str, Any] = {}

    path = config_path
    if path is None and env.get(f"{ENV_PREFIX}CONFIG"):
        path = Path(env[f"{ENV_PREFIX}CONFIG"]).expanduser()
    if path is None and DEFAULT_CONFIG_PATH.is_file():
        path = DEFAULT_CONFIG_PATH
    if path is not None:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError("config file not found") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError("config file is not valid TOML") from exc
        for key, raw in data.items():
            if key in SECRET_FIELDS:
                raise ConfigError(f"{key} must come from the environment, not the config file")
            if key not in names:
                raise ConfigError(f"unknown config key: {key}")
            values[key] = _coerce(key, raw, hints[key])

    for name in names:
        raw_env = env.get(ENV_PREFIX + name.upper())
        if raw_env is not None:
            values[name] = _coerce(name, raw_env, hints[name])

    for name, raw in overrides.items():
        if name not in names:
            raise ConfigError(f"unknown setting: {name}")
        if raw is not None:
            values[name] = _coerce(name, raw, hints[name])

    return _validate(Settings(**values))
