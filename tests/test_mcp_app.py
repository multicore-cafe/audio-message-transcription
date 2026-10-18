"""MCP service settings, startup checks and the Telegram client it creates."""

import asyncio
import dataclasses
import logging
import os
import signal
import socket
from pathlib import Path

import httpx2
import pytest
from telethon import functions

from tests.mcp_fakes import FakeTelegramClient, user
from tests.test_mcp_server import HEADERS, INITIALIZE
from transcription_bot.app import EXIT_CONFIG, EXIT_FAILURE, EXIT_OK
from transcription_bot.config import ConfigError
from transcription_bot.mcp_server import app as mcp_app
from transcription_bot.mcp_server.config import McpSettings
from transcription_bot.mcp_server.guard import GuardedMTProtoSender, ReadOnlyTelegramClient

ENV = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "hash"}


def settings(tmp_path, **extra):
    return McpSettings.from_env({**ENV, "DATA_DIR": str(tmp_path), "MCP_EXPORT_DIR": str(tmp_path / "exports"),
                                 **extra})


def test_defaults(tmp_path):
    s = McpSettings.from_env({**ENV, "DATA_DIR": str(tmp_path)})
    assert s.telegram.session_path == tmp_path / "telegram_mcp.session"
    assert (s.host, s.port) == ("127.0.0.1", 8790)
    assert s.export_dir == Path("exports") and s.export_host_dir == Path("exports").absolute()
    assert s.max_response_chars == 60_000 and s.media_max_bytes == 50 * 1024 * 1024


def test_overrides(tmp_path):
    s = settings(tmp_path, MCP_PORT="9001", MCP_HOST="0.0.0.0", MCP_EXPORT_HOST_DIR="/home/me/exports",
                 TELEGRAM_MCP_SESSION="other", MCP_ALLOWED_HOSTS="mcp.local:*, box:8790")
    assert (s.port, s.host, s.export_host_dir) == (9001, "0.0.0.0", Path("/home/me/exports"))
    assert s.telegram.session_path == tmp_path / "other.session"
    assert s.allowed_hosts == ["mcp.local:*", "box:8790"]


@pytest.mark.parametrize("extra", [
    {"TELEGRAM_MCP_SESSION": "transcription_bot"},
    {"TELEGRAM_MCP_SESSION": "shared", "TELEGRAM_SESSION": "shared"},
])
def test_refuses_to_share_the_bot_session(tmp_path, extra):
    with pytest.raises(ConfigError, match="own session"):
        settings(tmp_path, **extra)


def test_invalid_values(tmp_path):
    with pytest.raises(ConfigError):
        settings(tmp_path, MCP_PORT="99999")
    with pytest.raises(ConfigError):
        settings(tmp_path, MCP_PORT="http")
    with pytest.raises(ConfigError):
        McpSettings.from_env({"DATA_DIR": str(tmp_path)})


def test_clients_are_read_only_and_do_not_receive_updates(tmp_path):
    s = settings(tmp_path)
    client = mcp_app.create_readonly_client(s.telegram)
    assert isinstance(client, ReadOnlyTelegramClient) and isinstance(client._sender, GuardedMTProtoSender)
    assert client._no_updates
    assert functions.auth.SignInRequest not in client._sender.allowed
    login_client = mcp_app.create_readonly_client(s.telegram, allow_login=True)
    assert functions.auth.SignInRequest in login_client._sender.allowed
    assert functions.messages.ReadHistoryRequest not in login_client._sender.allowed
    assert s.telegram.session_path.parent == tmp_path


async def test_serve_without_session_explains_how_to_log_in(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    code = await mcp_app.serve(settings(tmp_path))
    assert code == EXIT_CONFIG
    assert f"Telegram session {tmp_path / 'telegram_mcp.session'} is not authorized" in caplog.text
    assert "docker compose run --rm mcp telegram-mcp login" in caplog.text


async def test_serve_with_revoked_session_explains_how_to_log_in(tmp_path, caplog, monkeypatch):
    class Unauthorized:
        class session:
            auth_key = object()

        connected = disconnected = False

        async def connect(self):
            self.connected = True

        async def is_user_authorized(self):
            return False

        async def disconnect(self):
            self.disconnected = True

    fake = Unauthorized()
    monkeypatch.setattr(mcp_app, "create_readonly_client", lambda telegram: fake)
    caplog.set_level(logging.ERROR)
    assert await mcp_app.serve(settings(tmp_path)) == EXIT_CONFIG
    assert fake.connected and fake.disconnected
    assert "is not authorized. Log in interactively with `docker compose run --rm mcp telegram-mcp login`" in caplog.text


class AuthorizedClient(FakeTelegramClient):
    """Fake account that also looks like a connected, logged-in Telethon client."""

    class session:
        auth_key = object()

    def __init__(self):
        super().__init__()
        self.disconnected = asyncio.get_running_loop().create_future()
        self.closed = False
        self.flood_sleep_threshold = 60

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    def is_connected(self):
        return not self.closed

    async def disconnect(self):
        self.closed = True


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for_server(port):
    async with httpx2.AsyncClient() as http:
        for _ in range(100):
            try:
                return await http.post(f"http://127.0.0.1:{port}/mcp", json=INITIALIZE, headers=HEADERS)
            except httpx2.TransportError:
                await asyncio.sleep(0.05)
    raise AssertionError("server did not start")


async def test_serve_runs_http_until_sigterm(tmp_path, monkeypatch):
    fake = AuthorizedClient()
    fake.add_dialog(user(1, "Alice"))
    monkeypatch.setattr(mcp_app, "create_readonly_client", lambda telegram: fake)
    port = free_port()
    task = asyncio.create_task(mcp_app.serve(dataclasses.replace(settings(tmp_path), port=port)))
    response = await wait_for_server(port)
    assert response.status_code == 200 and response.json()["result"]["serverInfo"]["name"] == "telegram-readonly"
    assert fake.flood_sleep_threshold == 30
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 15) == EXIT_OK
    assert fake.closed


async def test_serve_exits_when_telegram_is_gone_for_good(tmp_path, monkeypatch, caplog):
    fake = AuthorizedClient()
    monkeypatch.setattr(mcp_app, "create_readonly_client", lambda telegram: fake)
    port = free_port()
    task = asyncio.create_task(mcp_app.serve(dataclasses.replace(settings(tmp_path), port=port)))
    await wait_for_server(port)
    fake.disconnected.set_result(None)
    assert await asyncio.wait_for(task, 15) == EXIT_FAILURE
    assert "Telegram connection lost for good" in caplog.text
