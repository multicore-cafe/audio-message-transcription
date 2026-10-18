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
MB = 1024 * 1024


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
    media_max_total_bytes: int
    max_response_chars: int
    dialog_cache_seconds: float
    flood_sleep_threshold: int
    auth_token: str | None

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
            media_max_bytes=int(_number(env, "MCP_MEDIA_MAX_MB", 50, float) * MB),
            media_max_total_bytes=int(_number(env, "MCP_EXPORT_MAX_TOTAL_MB", 1000, float) * MB),
            max_response_chars=_number(env, "MCP_MAX_RESPONSE_CHARS", 60_000),
            dialog_cache_seconds=_number(env, "MCP_DIALOG_CACHE_SECONDS", 60, float),
            flood_sleep_threshold=_number(env, "MCP_FLOOD_SLEEP_THRESHOLD", 30),
            auth_token=_get(env, "MCP_AUTH_TOKEN") or None,
        )
        if not 0 < settings.port < 65536:
            raise ConfigError(f"MCP_PORT must be a TCP port, got {settings.port}")
        if settings.export_max_messages < 1 or settings.max_response_chars < 1000:
            raise ConfigError("MCP_EXPORT_MAX_MESSAGES or MCP_MAX_RESPONSE_CHARS (>= 1000) is too small")
        return settings
