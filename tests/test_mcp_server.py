"""The MCP server itself: tool list, calls through an MCP client, HTTP transport and its Host checks."""

import contextlib
import json

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from tests.mcp_fakes import FakeTelegramClient, basic_group, user
from transcription_bot.mcp_server.export import Exporter
from transcription_bot.mcp_server.reader import TelegramReader
from transcription_bot.mcp_server.server import build_server, http_app

TOOLS = {
    "list_folders", "list_folder_chats", "list_dialogs", "get_chat_info", "list_forum_topics", "read_messages",
    "get_messages", "search_messages", "search_global", "export_chat",
}


@pytest.fixture
def server(tmp_path):
    client = FakeTelegramClient()
    alice = client.add_dialog(user(1, "Alice", username="alice"), unread=1)
    group = client.add_dialog(basic_group(10, "Family"))
    for i in range(1, 6):
        client.add_message(group, i, f"hello {i}", sender=alice)
    reader = TelegramReader(client)
    return build_server(reader, Exporter(reader, tmp_path, tmp_path))


def payload(result):
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


async def test_tools_are_listed_with_read_only_annotations(server):
    async with Client(server) as mcp:
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
    assert set(tools) == TOOLS
    for name, tool in tools.items():
        assert tool.description
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.read_only_hint is (name != "export_chat")  # export writes local files only
    schema = tools["read_messages"].input_schema
    assert schema["required"] == ["chat"]
    assert schema["properties"]["limit"]["maximum"] == 100


async def test_calling_tools_in_process(server):
    async with Client(server) as mcp:
        dialogs = payload(await mcp.call_tool("list_dialogs", {"unread_only": True}))
        assert [d["name"] for d in dialogs["dialogs"]] == ["Alice"]
        messages = payload(await mcp.call_tool("read_messages", {"chat": "Family", "limit": 2}))
        assert [m["text"] for m in messages["messages"]] == ["hello 5", "hello 4"]
        assert messages["next_offset_id"] == 4
        missing = await mcp.call_tool("get_chat_info", {"chat": "Nobody"})
        assert missing.is_error and "no chat with the title 'Nobody'" in missing.content[0].text
        too_many = await mcp.call_tool("read_messages", {"chat": "Family", "limit": 5000})
        assert too_many.is_error


@contextlib.asynccontextmanager
async def running(server):
    app = http_app(server)
    async with app.router.lifespan_context(app):
        yield app


def asgi_client(app, host):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=f"http://{host}")


async def test_mcp_client_over_streamable_http(server):
    async with running(server) as app, asgi_client(app, "127.0.0.1:8790") as http_client:
        async with Client(streamable_http_client("http://127.0.0.1:8790/mcp", http_client=http_client)) as mcp:
            names = {tool.name for tool in (await mcp.list_tools()).tools}
            assert names == TOOLS
            result = payload(await mcp.call_tool("search_messages", {"chat": -10, "query": "hello 3"}))
            assert [m["id"] for m in result["messages"]] == [3]


INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.mark.parametrize("host", ["127.0.0.1:8790", "localhost:8790"])
async def test_loopback_hosts_are_accepted(server, host):
    async with running(server) as app, asgi_client(app, host) as client:
        response = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
    assert response.status_code == 200


async def test_dns_rebinding_is_blocked(server):
    async with running(server) as app, asgi_client(app, "evil.example:8790") as client:
        bad_host = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
    async with running(server) as app, asgi_client(app, "127.0.0.1:8790") as client:
        bad_origin = await client.post("/mcp", json=INITIALIZE, headers={**HEADERS, "Origin": "http://evil.example"})
    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403
