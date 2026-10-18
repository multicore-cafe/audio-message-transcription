from pathlib import Path

import pytest

from transcription_bot.config import BotSettings, ConfigError, TelegramSettings

ENV = {"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "hash", "OPENAI_API_KEY": "key", "DATA_DIR": "/data"}


def test_defaults():
    settings = BotSettings.from_env(ENV)
    assert settings.telegram.api_id == 123
    assert settings.telegram.session_path == Path("/data/transcription_bot.session")
    assert settings.queue_path == Path("/data/queue.sqlite3")
    assert settings.transcription_model == "whisper-1"


def test_session_is_configurable_per_service():
    env = {**ENV, "TELEGRAM_SESSION": "bot2", "MCP_SESSION": "/elsewhere/mcp.session"}
    assert TelegramSettings.from_env(env).session_path == Path("/data/bot2.session")
    assert TelegramSettings.from_env(env, session_var="MCP_SESSION").session_path == Path("/elsewhere/mcp.session")


@pytest.mark.parametrize("missing", ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "OPENAI_API_KEY"])
def test_missing_variable_is_named(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        BotSettings.from_env(env)


def test_invalid_api_id():
    with pytest.raises(ConfigError, match="TELEGRAM_API_ID"):
        BotSettings.from_env({**ENV, "TELEGRAM_API_ID": "abc"})
