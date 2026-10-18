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
# One tool response must fit into the local model's 65k-token context together with the conversation.
DEFAULT_MAX_RESPONSE_CHARS = 16_000
# Export files live outside the repository, in a 0700 directory under the owner's home.
DEFAULT_EXPORT_DIR = Path("~/.local/share/telegram-mcp/exports")
MIN_TOKEN_LENGTH = 32


DISABLED_MESSAGE = "Telegram MCP is disabled: set TELEGRAM_MCP_ENABLED=1 in .env to enable it"


def _list(env: Mapping[str, str], name: str) -> list[str]:
    return [item.strip() for item in _get(env, name).split(",") if item.strip()]


def mcp_enabled(env: Mapping[str, str] | None = None) -> bool:
    """The MCP service is off by default; only the exact value `1` enables it."""
    env = os.environ if env is None else env
    return _get(env, "TELEGRAM_MCP_ENABLED") == "1"


def _secret(env: Mapping[str, str], name: str) -> str | None:
    """Read a secret from `<name>_FILE` (preferred, used by Docker secrets) or from the variable itself."""
    path = _get(env, f"{name}_FILE")
    if path:
        try:
            value = Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read the secret file {path} ({name}_FILE): {exc}") from None
        if not value:
            raise ConfigError(f"the secret file {path} ({name}_FILE) is empty")
        return value
    return _get(env, name) or None


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
        export_dir = Path(_get(env, "MCP_EXPORT_DIR") or DEFAULT_EXPORT_DIR).expanduser()
        auth_token = _secret(env, "MCP_AUTH_TOKEN")
        if not auth_token:
            raise ConfigError(
                "refusing to start without a bearer token: set MCP_AUTH_TOKEN_FILE to a file outside the "
                "repository (e.g. ~/.config/telegram-mcp/token, mode 0600) or, as a fallback, MCP_AUTH_TOKEN. "
                "Generate one with `openssl rand -hex 32`."
            )
        if len(auth_token) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"the MCP bearer token must be at least {MIN_TOKEN_LENGTH} characters long")
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
            max_response_chars=_number(env, "MCP_MAX_RESPONSE_CHARS", DEFAULT_MAX_RESPONSE_CHARS),
            dialog_cache_seconds=_number(env, "MCP_DIALOG_CACHE_SECONDS", 60, float),
            flood_sleep_threshold=_number(env, "MCP_FLOOD_SLEEP_THRESHOLD", 30),
            auth_token=auth_token,
        )
        if not 0 < settings.port < 65536:
            raise ConfigError(f"MCP_PORT must be a TCP port, got {settings.port}")
        if settings.export_max_messages < 1 or settings.max_response_chars < 1000:
            raise ConfigError("MCP_EXPORT_MAX_MESSAGES or MCP_MAX_RESPONSE_CHARS (>= 1000) is too small")
        return settings
