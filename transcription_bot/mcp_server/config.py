"""Settings of the read-only Telegram MCP server."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import ConfigError, TelegramSettings, _get, _number

DEFAULT_PORT = 8790
DEFAULT_SESSION = "telegram_mcp"
BOT_DEFAULT_SESSION = "transcription_bot"


def _list(env: Mapping[str, str], name: str) -> list[str]:
    return [item.strip() for item in _get(env, name).split(",") if item.strip()]


@dataclass(frozen=True)
class McpSettings:
    telegram: TelegramSettings
    host: str
    port: int
    allowed_hosts: list[str]
    allowed_origins: list[str]
    export_dir: Path
    export_host_dir: Path
    export_max_messages: int
    media_max_bytes: int
    max_response_chars: int
    dialog_cache_seconds: float
    flood_sleep_threshold: int
    watchdog_interval: float
    watchdog_timeout: float
    watchdog_max_failures: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> McpSettings:
        env = os.environ if env is None else env
        telegram = TelegramSettings.from_env(env, session_var="TELEGRAM_MCP_SESSION", default_session=DEFAULT_SESSION)
        bot_session = TelegramSettings.from_env(env).session_path
        if telegram.session_path.resolve() == bot_session.resolve():
            raise ConfigError(
                f"TELEGRAM_MCP_SESSION points at the bot's session {bot_session}; the MCP server needs its own session"
            )
        export_dir = Path(_get(env, "MCP_EXPORT_DIR") or "exports")
        settings = cls(
            telegram=telegram,
            host=_get(env, "MCP_HOST") or "127.0.0.1",
            port=_number(env, "MCP_PORT", DEFAULT_PORT),
            allowed_hosts=_list(env, "MCP_ALLOWED_HOSTS"),
            allowed_origins=_list(env, "MCP_ALLOWED_ORIGINS"),
            export_dir=export_dir,
            export_host_dir=Path(_get(env, "MCP_EXPORT_HOST_DIR") or export_dir.absolute()),
            export_max_messages=_number(env, "MCP_EXPORT_MAX_MESSAGES", 50_000),
            media_max_bytes=int(_number(env, "MCP_MEDIA_MAX_MB", 50, float) * 1024 * 1024),
            max_response_chars=_number(env, "MCP_MAX_RESPONSE_CHARS", 60_000),
            dialog_cache_seconds=_number(env, "MCP_DIALOG_CACHE_SECONDS", 60, float),
            flood_sleep_threshold=_number(env, "MCP_FLOOD_SLEEP_THRESHOLD", 30),
            watchdog_interval=_number(env, "WATCHDOG_INTERVAL", 60, float),
            watchdog_timeout=_number(env, "WATCHDOG_TIMEOUT", 30, float),
            watchdog_max_failures=_number(env, "WATCHDOG_MAX_FAILURES", 3),
        )
        if not 0 < settings.port < 65536:
            raise ConfigError(f"MCP_PORT must be a TCP port, got {settings.port}")
        if settings.export_max_messages < 1 or settings.max_response_chars < 1000 or settings.watchdog_max_failures < 1:
            raise ConfigError("MCP_EXPORT_MAX_MESSAGES, MCP_MAX_RESPONSE_CHARS (>= 1000) and WATCHDOG_MAX_FAILURES are too small")
        return settings
