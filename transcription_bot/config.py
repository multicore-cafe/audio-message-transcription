"""Settings read from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Required settings are missing or invalid."""


def _get(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _require(env: Mapping[str, str], name: str) -> str:
    value = _get(env, name)
    if not value:
        raise ConfigError(f"environment variable {name} is required (see .env.example)")
    return value


def _number(env: Mapping[str, str], name: str, default: float, kind: type = int):
    value = _get(env, name)
    if not value:
        return kind(default)
    try:
        return kind(value)
    except ValueError:
        raise ConfigError(f"environment variable {name} must be a number, got {value!r}") from None


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Directory for the session and queue files (`/data` in Docker)."""
    env = os.environ if env is None else env
    return Path(_get(env, "DATA_DIR") or "data")


def _in_data_dir(value: str, env: Mapping[str, str]) -> Path:
    path = Path(value)
    return path if path.is_absolute() else data_dir(env) / path


@dataclass(frozen=True)
class TelegramSettings:
    api_id: int
    api_hash: str
    session_path: Path
    connection_retries: int = 5
    retry_delay: int = 5
    request_timeout: int = 30

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        session_var: str = "TELEGRAM_SESSION",
        default_session: str = "transcription_bot",
    ) -> TelegramSettings:
        """Read Telegram settings; `session_var` lets other services use their own session."""
        env = os.environ if env is None else env
        api_id = _require(env, "TELEGRAM_API_ID")
        if not api_id.isdigit():
            raise ConfigError(f"environment variable TELEGRAM_API_ID must be an integer, got {api_id!r}")
        session = _get(env, session_var) or default_session
        if not session.endswith(".session"):
            session += ".session"
        return cls(
            api_id=int(api_id),
            api_hash=_require(env, "TELEGRAM_API_HASH"),
            session_path=_in_data_dir(session, env),
            connection_retries=_number(env, "TELEGRAM_CONNECTION_RETRIES", 5),
            retry_delay=_number(env, "TELEGRAM_RETRY_DELAY", 5),
            request_timeout=_number(env, "TELEGRAM_TIMEOUT", 30),
        )


@dataclass(frozen=True)
class BotSettings:
    telegram: TelegramSettings
    openai_api_key: str
    openai_organization: str | None
    transcription_model: str
    openai_timeout: float
    queue_path: Path
    workers: int
    max_attempts: int
    retry_base_delay: float
    retry_max_delay: float
    processing_timeout: float
    watchdog_interval: float
    watchdog_timeout: float
    watchdog_max_failures: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BotSettings:
        env = os.environ if env is None else env
        settings = cls(
            telegram=TelegramSettings.from_env(env),
            openai_api_key=_require(env, "OPENAI_API_KEY"),
            openai_organization=_get(env, "OPENAI_ORGANIZATION") or None,
            transcription_model=_get(env, "TRANSCRIPTION_MODEL") or "whisper-1",
            openai_timeout=_number(env, "OPENAI_TIMEOUT", 300, float),
            queue_path=_in_data_dir(_get(env, "QUEUE_DB") or "queue.sqlite3", env),
            workers=_number(env, "WORKERS", 2),
            max_attempts=_number(env, "MAX_ATTEMPTS", 5),
            retry_base_delay=_number(env, "RETRY_BASE_DELAY", 10, float),
            retry_max_delay=_number(env, "RETRY_MAX_DELAY", 300, float),
            processing_timeout=_number(env, "PROCESSING_TIMEOUT", 900, float),
            watchdog_interval=_number(env, "WATCHDOG_INTERVAL", 60, float),
            watchdog_timeout=_number(env, "WATCHDOG_TIMEOUT", 30, float),
            watchdog_max_failures=_number(env, "WATCHDOG_MAX_FAILURES", 3),
        )
        if settings.workers < 1 or settings.max_attempts < 1 or settings.watchdog_max_failures < 1:
            raise ConfigError("WORKERS, MAX_ATTEMPTS and WATCHDOG_MAX_FAILURES must be at least 1")
        return settings
