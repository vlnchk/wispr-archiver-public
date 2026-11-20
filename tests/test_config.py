from __future__ import annotations

from pathlib import Path

import pytest

from wispr_archiver.config import load_settings
from wispr_archiver.errors import ConfigError

NO_FILE = Path("/nonexistent/wispr-archiver-test.toml")


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_are_safe() -> None:
    s = load_settings(None, env={})
    assert s.requests_per_second == 1.0
    assert s.chunk_size == 40_000
    assert s.timezone == "UTC"
    assert s.search_limit == 200
    assert s.max_attempts == 5
    assert s.overlap_hours == 24
    assert s.shrink_threshold == pytest.approx(0.10)
    assert s.rate_limit_pause_seconds >= 60
    assert s.lock_path == s.token_path.parent / "run.lock"
    assert s.telegram_bot_token is None


def test_precedence_defaults_toml_env_cli(tmp_path: Path) -> None:
    cfg = write(tmp_path, 'archive_dir = "/srv/a"\ntimezone = "Europe/Paris"\nmax_attempts = 3\n')
    s = load_settings(cfg, env={"WISPR_ARCHIVER_MAX_ATTEMPTS": "4"})
    assert s.archive_dir == Path("/srv/a")
    assert s.timezone == "Europe/Paris"
    assert s.max_attempts == 4  # env beats TOML
    s2 = load_settings(
        cfg, env={"WISPR_ARCHIVER_ARCHIVE_DIR": "/srv/b"}, archive_dir=Path("/srv/c")
    )
    assert s2.archive_dir == Path("/srv/c")  # CLI beats env
    s3 = load_settings(cfg, env={}, archive_dir=None)
    assert s3.archive_dir == Path("/srv/a")  # None override is ignored


def test_config_path_from_env(tmp_path: Path) -> None:
    cfg = write(tmp_path, "login_port = 40000\n")
    s = load_settings(None, env={"WISPR_ARCHIVER_CONFIG": str(cfg)})
    assert s.login_port == 40000


def test_secret_only_from_env(tmp_path: Path) -> None:
    cfg = write(tmp_path, 'telegram_bot_token = "123:abc"\n')
    with pytest.raises(ConfigError, match="environment"):
        load_settings(cfg, env={})
    s = load_settings(None, env={"WISPR_ARCHIVER_TELEGRAM_BOT_TOKEN": "123:abc"})
    assert s.telegram_bot_token == "123:abc"
    assert "123:abc" not in repr(s)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("requests_per_second", "2"),
        ("chunk_size", "40001"),
        ("search_limit", "201"),
        ("rate_limit_pause_seconds", "10"),
        ("shrink_threshold", "0.1001"),
        ("shrink_threshold", "1.5"),
        ("max_attempts", "0"),
        ("timezone", "Mars/Olympus"),
        ("max_attempts", "five"),
    ],
)
def test_limits_cannot_be_loosened(key: str, value: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(None, env={f"WISPR_ARCHIVER_{key.upper()}": value})


def test_unknown_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown config key"):
        load_settings(write(tmp_path, "mcp_url = 'https://example.com'\n"), env={})


def test_missing_file_rejected() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(NO_FILE, env={})


def test_optional_path_empty_means_none() -> None:
    s = load_settings(None, env={"WISPR_ARCHIVER_RCLONE_CONFIG": ""})
    assert s.rclone_config is None
