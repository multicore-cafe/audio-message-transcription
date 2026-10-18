"""Settings read from environment variables: credentials, model, session name and data directory.

Timeouts, retry limits and the watchdog are module constants (see processor.py, telegram.py,
transcriber.py and watchdog.py); tests pass other values to the constructors.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

QUEUE_FILE = "queue.sqlite3"


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
        return cls(api_id=int(api_id), api_hash=_require(env, "TELEGRAM_API_HASH"),
                   session_path=_in_data_dir(session, env))


@dataclass(frozen=True)
class BotSettings:
    telegram: TelegramSettings
    openai_api_key: str
    openai_organization: str | None
    transcription_model: str
    queue_path: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BotSettings:
        env = os.environ if env is None else env
        return cls(
            telegram=TelegramSettings.from_env(env),
            openai_api_key=_require(env, "OPENAI_API_KEY"),
            openai_organization=_get(env, "OPENAI_ORGANIZATION") or None,
            transcription_model=_get(env, "TRANSCRIPTION_MODEL") or "whisper-1",
            queue_path=data_dir(env) / QUEUE_FILE,
        )
