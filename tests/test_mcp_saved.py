"""Opt-in Saved Messages writes: send_to_saved / forward_to_saved with the fake account."""

import asyncio
import dataclasses
import json
import os
import signal
from pathlib import Path

import httpx2

import pytest
from mcp import Client
from telethon import errors, functions, types

from tests.mcp_fakes import FakeTelegramClient, channel, user
from tests.test_mcp_app import AUTH, AuthorizedClient, free_port, settings, wait_for_server
from tests.test_mcp_server import HEADERS, TOOLS, payload
from transcription_bot.app import EXIT_OK
from transcription_bot.mcp_server import app as mcp_app
from transcription_bot.mcp_server import saved as saved_module
from transcription_bot.mcp_server import server as server_module
from transcription_bot.mcp_server.allowlist import REFUSAL, AllowList, access_policy
from transcription_bot.mcp_server.config import saved_messages_write_enabled
from transcription_bot.mcp_server.export import Exporter
from transcription_bot.mcp_server.guard import READ_ONLY_REQUESTS, SAVED_MESSAGES_REQUESTS
from transcription_bot.mcp_server.reader import ReaderError, TelegramReader
from transcription_bot.mcp_server.saved import MAX_CHUNK, SavedMessagesWriter, split_text
from transcription_bot.mcp_server.server import build_server

ALLOW_LIST = AllowList.parse("oneprovider/my-model\n", source="test")
POLICY = access_policy(ALLOW_LIST)
SAVED_TOOLS = {"send_to_saved", "forward_to_saved"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def account():
    client = FakeTelegramClient()
    client.allowed = READ_ONLY_REQUESTS | SAVED_MESSAGES_REQUESTS
    alice = client.add_dialog(user(1, "Alice", username="alice"))
    news = client.add_dialog(channel(50, "News", username="news"))
    secret = client.add_dialog(channel(60, "Secret", username="secretchan"))
    secret.noforwards = True
    for i in range(1, 6):
        client.add_message(news, i, f"post {i}")
        client.add_message(alice, i, f"hi {i}", sender=alice)
    return client


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def writer(account, clock):
    return SavedMessagesWriter(TelegramReader(account), clock=clock)


def server_for(client, tmp_path, writer=None):
    reader = TelegramReader(client) if writer is None else writer.reader
    return build_server(reader, Exporter(reader, tmp_path, tmp_path), ALLOW_LIST, writer)


def saved_texts(client):
    return [m.message for m in client.saved()]


# Flag and wiring


def test_flag_is_off_unless_exactly_one():
    assert not saved_messages_write_enabled({})
    for value in ("0", "true", "yes", "on", "01", ""):
        assert not saved_messages_write_enabled({"MCP_SAVED_MESSAGES_WRITE": value}), value
    assert saved_messages_write_enabled({"MCP_SAVED_MESSAGES_WRITE": "1"})


def test_settings_and_client_follow_the_flag(tmp_path):
    off = settings(tmp_path)
    assert off.saved_messages_write is False
    on = settings(tmp_path, MCP_SAVED_MESSAGES_WRITE="1")
    assert on.saved_messages_write is True
    plain = mcp_app.create_readonly_client(off.telegram)
    assert not SAVED_MESSAGES_REQUESTS & plain._sender.allowed
    writing = mcp_app.create_readonly_client(on.telegram, saved_messages_write=True)
    assert writing._sender.allowed == READ_ONLY_REQUESTS | SAVED_MESSAGES_REQUESTS
    login = mcp_app.create_readonly_client(on.telegram, allow_login=True)
    assert not SAVED_MESSAGES_REQUESTS & login._sender.allowed


async def test_flag_off_means_no_tools(account, tmp_path):
    async with Client(server_for(account, tmp_path)) as mcp:
        names = {tool.name for tool in (await mcp.list_tools()).tools}
        assert names == TOOLS
        result = await mcp.call_tool("send_to_saved", {"text": "note"})
        assert result.is_error
    assert account.saved() == []


async def test_flag_off_guard_rejects_saved_messages_sends(tmp_path):
    """Even if a writer were wired up, the read-only guard alone refuses the writes."""
    client = FakeTelegramClient()  # allowed = READ_ONLY_REQUESTS
    client.add_dialog(channel(50, "News", username="news"))
    client.add_message(client.entities[-1000000000050], 1, "post")
    writer = SavedMessagesWriter(TelegramReader(client))
    async with Client(server_for(client, tmp_path, writer)) as mcp:
        sent = await mcp.call_tool("send_to_saved", {"text": "note"})
        forwarded = await mcp.call_tool("forward_to_saved", {"chat": "@news", "message_ids": [1]})
    for result in (sent, forwarded):
        assert result.is_error and "blocked by the read-only guard" in result.content[0].text
    assert client.saved() == []


@pytest.mark.parametrize("flag", ["0", "1"])
async def test_serve_registers_the_tools_only_with_the_flag(tmp_path, monkeypatch, flag):
    fake = AuthorizedClient()
    created = {}

    def create(telegram, **kwargs):
        created.update(kwargs)
        return fake

    monkeypatch.setattr(mcp_app, "create_readonly_client", create)
    port = free_port()
    config = dataclasses.replace(settings(tmp_path, MCP_SAVED_MESSAGES_WRITE=flag), port=port)
    task = asyncio.create_task(mcp_app.serve(config))
    await wait_for_server(port)
    async with httpx2.AsyncClient() as http:
        response = await http.post(f"http://127.0.0.1:{port}/mcp", headers={**HEADERS, **AUTH},
                                   json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 15) == EXIT_OK
    assert created == {"saved_messages_write": flag == "1"}
    assert names == (TOOLS | SAVED_TOOLS if flag == "1" else TOOLS)


async def test_tools_are_listed_with_write_annotations(writer, account, tmp_path):
    server = server_for(account, tmp_path, writer)
    async with Client(server) as mcp:
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
    assert set(tools) == TOOLS | SAVED_TOOLS
    for name in SAVED_TOOLS:
        tool = tools[name]
        assert tool.description.startswith(POLICY) and REFUSAL in tool.description
        assert "Saved Messages" in tool.description
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is False
    assert tools["send_to_saved"].input_schema["properties"]["text"]["maxLength"] == 16_000
    ids = tools["forward_to_saved"].input_schema["properties"]["message_ids"]
    assert ids["maxItems"] == 100 and ids["minItems"] == 1
    assert server.instructions.startswith(POLICY)
    assert "except that send_to_saved and forward_to_saved" in server.instructions
    assert "Nothing can be sent" not in server.instructions


def test_docstring_limits_match_the_constants():
    source = Path(server_module.__file__).read_text(encoding="utf-8")
    assert f"at most {saved_module.MAX_TEXT_PER_CALL} per call" in source
    assert f"up to {saved_module.MAX_CHUNK} characters" in source
    assert f"Up to {saved_module.MAX_FORWARD_IDS} ids per call" in source
    assert source.count(f"At most {saved_module.RATE_LIMIT_CALLS} Saved Messages writes per minute") == 2
    assert saved_module.RATE_LIMIT_WINDOW == 60


# send_to_saved


async def test_send_returns_ids_only(writer, account, tmp_path):
    async with Client(server_for(account, tmp_path, writer)) as mcp:
        result = payload(await mcp.call_tool("send_to_saved", {"text": "Buy **milk** https://example.com"}))
    assert result == {"message_ids": [1]}
    assert saved_texts(account) == ["Buy **milk** https://example.com"]  # no markdown parsing
    request = account.requests[-1]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert isinstance(request.peer, types.InputPeerSelf) and not request.entities and request.no_webpage


async def test_long_text_is_split_into_chunks(writer, account):
    lines = [f"line {i:04d} " + "x" * 90 for i in range(140)]  # ~14 000 characters
    text = "\n".join(lines)
    result = await writer.send_to_saved(text)
    texts = saved_texts(account)
    assert result == {"message_ids": list(range(1, len(texts) + 1))}
    assert len(texts) == 4
    assert all(len(t) <= MAX_CHUNK for t in texts)
    assert "".join(texts) == text
    assert all(t.endswith("\n") for t in texts[:-1])  # split at line breaks


def test_split_text_edge_cases():
    assert split_text("short") == ["short"]
    assert split_text("x" * 9000) == ["x" * 4096, "x" * 4096, "x" * 808]
    words = " ".join(["word"] * 2000)
    parts = split_text(words)
    assert "".join(parts) == words and all(len(p) <= 4096 and not p.startswith(" ") for p in parts)
    # Astral characters count twice towards Telegram's UTF-16 limit.
    emoji = "😀" * 3000
    parts = split_text(emoji)
    assert "".join(parts) == emoji
    assert all(len(p.encode("utf-16-le")) // 2 <= 4096 for p in parts) and len(parts) == 2
    # Whitespace-only pieces are dropped: Telegram trims messages and refuses empty ones.
    assert split_text("a" * 4096 + " " * 4096 + "b") == ["a" * 4096, "b"]


async def test_send_limits(writer, account, tmp_path):
    async with Client(server_for(account, tmp_path, writer)) as mcp:
        too_long = await mcp.call_tool("send_to_saved", {"text": "x" * 16_001})
        empty = await mcp.call_tool("send_to_saved", {"text": ""})
        blank = await mcp.call_tool("send_to_saved", {"text": "  \n "})
        exact = await mcp.call_tool("send_to_saved", {"text": "x" * 16_000})
    assert too_long.is_error and empty.is_error
    assert blank.is_error and "text must not be empty" in blank.content[0].text
    assert payload(exact) == {"message_ids": [1, 2, 3, 4]}
    with pytest.raises(ReaderError, match="at most 16000"):
        await writer.send_to_saved("x" * 16_001)


async def test_partial_send_reports_the_ids_already_sent(writer, account):
    real = account.__call__
    calls = 0

    async def flaky(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise errors.MessageTooLongError(request)
        return await real(request)

    writer.client = flaky
    with pytest.raises(ReaderError, match=r"sent 1 of 3 parts \(message_ids \[1\]\), then Telegram error"):
        await writer.send_to_saved("x" * 9000)


async def test_send_error_on_the_first_part_is_a_tool_error(writer, account, tmp_path):
    account.fail["SendMessageRequest"] = errors.FloodWaitError(None, capture=42)
    async with Client(server_for(account, tmp_path, writer)) as mcp:
        result = await mcp.call_tool("send_to_saved", {"text": "note"})
    assert result.is_error and "retry in 42 s" in result.content[0].text


# forward_to_saved


async def test_forward_returns_new_ids_only(writer, account, tmp_path):
    async with Client(server_for(account, tmp_path, writer)) as mcp:
        result = payload(await mcp.call_tool("forward_to_saved", {"chat": "@news", "message_ids": [3, 1, 3]}))
        from_alice = payload(await mcp.call_tool("forward_to_saved", {"chat": 1, "message_ids": [5]}))
    assert result == {"message_ids": [1, 2]}
    assert from_alice == {"message_ids": [3]}
    assert "post" not in json.dumps(result)
    assert saved_texts(account) == ["post 3", "post 1", "hi 5"]
    assert all(m.fwd_from is not None for m in account.saved())
    forward = [r for r in account.requests if isinstance(r, functions.messages.ForwardMessagesRequest)][0]
    assert isinstance(forward.to_peer, types.InputPeerSelf)
    assert isinstance(forward.from_peer, types.InputPeerChannel) and forward.id == [3, 1]


async def test_forward_errors(writer, account, tmp_path):
    account.messages[-1000000000050][1].noforwards = True  # message 2 of News
    async with Client(server_for(account, tmp_path, writer)) as mcp:
        missing = await mcp.call_tool("forward_to_saved", {"chat": "@news", "message_ids": [1, 99, 98]})
        protected_chat = await mcp.call_tool("forward_to_saved", {"chat": "@secretchan", "message_ids": [1]})
        protected_message = await mcp.call_tool("forward_to_saved", {"chat": "@news", "message_ids": [1, 2]})
        unknown_chat = await mcp.call_tool("forward_to_saved", {"chat": "Nobody", "message_ids": [1]})
        too_many = await mcp.call_tool("forward_to_saved", {"chat": "@news", "message_ids": list(range(1, 102))})
        none = await mcp.call_tool("forward_to_saved", {"chat": "@news", "message_ids": []})
        negative = await mcp.call_tool("forward_to_saved", {"chat": "@news", "message_ids": [0]})
        account.fail["ForwardMessagesRequest"] = errors.ChatForwardsRestrictedError(None)
        restricted = await mcp.call_tool("forward_to_saved", {"chat": "@news", "message_ids": [1]})
        account.fail["ForwardMessagesRequest"] = errors.MessageIdInvalidError(None)
        invalid = await mcp.call_tool("forward_to_saved", {"chat": "@news", "message_ids": [1]})

    def error(result):
        assert result.is_error
        return result.content[0].text

    assert "messages not found in this chat: [99, 98]" in error(missing)
    assert "protects its content" in error(protected_chat)
    assert "protected content cannot be forwarded: [2]" in error(protected_message)
    assert "no chat with the title 'Nobody'" in error(unknown_chat)
    error(too_many), error(none)
    assert "positive integers" in error(negative)
    assert "protects its content" in error(restricted)
    assert "did not find" in error(invalid)
    assert account.saved() == []


async def test_forward_service_messages_is_refused(writer, account):
    account.add_message(account.entities[-1000000000050], 10, action=types.MessageActionPinMessage())
    with pytest.raises(ReaderError, match=r"service messages cannot be forwarded: \[10\]"):
        await writer.forward_to_saved("@news", [10])


async def test_forward_from_saved_messages_itself(writer, account):
    await writer.send_to_saved("note")
    assert await writer.forward_to_saved("me", [1]) == {"message_ids": [2]}


# Rate limit


async def test_rate_limit(writer, account, clock, tmp_path):
    for i in range(20):
        await writer.send_to_saved(f"note {i}")
    with pytest.raises(ReaderError, match=r"rate limit: at most 20 Saved Messages writes per 60 s; retry in 60 s"):
        await writer.send_to_saved("one too many")
    with pytest.raises(ReaderError, match="rate limit"):
        await writer.forward_to_saved("@news", [1])
    clock.now += 30
    async with Client(server_for(account, tmp_path, writer)) as mcp:
        limited = await mcp.call_tool("send_to_saved", {"text": "still limited"})
    assert limited.is_error and "retry in 30 s" in limited.content[0].text
    clock.now += 30
    assert await writer.send_to_saved("allowed again") == {"message_ids": [21]}
    assert len(account.saved()) == 21


async def test_rejected_calls_do_not_use_the_rate_limit(writer, account):
    for _ in range(30):
        with pytest.raises(ReaderError):
            await writer.forward_to_saved("@news", [99])
        with pytest.raises(ReaderError):
            await writer.send_to_saved(" ")
    assert await writer.send_to_saved("fine") == {"message_ids": [1]}


async def test_chunked_send_counts_as_one_write(writer, account):
    for _ in range(20):
        await writer.send_to_saved("x" * 5000)
    assert len(account.saved()) == 40
    with pytest.raises(ReaderError, match="rate limit"):
        await writer.send_to_saved("x")
