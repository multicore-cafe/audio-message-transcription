"""Search exports: matches are paginated server-side and written to files, never through the result."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from telethon import functions

from tests.mcp_fakes import FakeTelegramClient, basic_group, channel, document, user, voice
from transcription_bot.mcp_server.export import Exporter
from transcription_bot.mcp_server.reader import ReaderError, TelegramReader

NOW = datetime(2026, 10, 18, 12, 30, tzinfo=timezone.utc)


@pytest.fixture
def client():
    c = FakeTelegramClient()
    c.alice = c.add_dialog(user(1, "Alice", username="alice"))
    c.group = c.add_dialog(basic_group(10, "Book club"))
    c.news = c.add_dialog(channel(30, "News"))
    return c


def exporter(client, tmp_path, **kwargs):
    (tmp_path / "exports").mkdir(exist_ok=True)
    return Exporter(TelegramReader(client), tmp_path / "exports", Path("/home/owner/exports"), now=lambda: NOW,
                    **kwargs)


def folder_of(result):
    return Path(result["directory"]).name


def part(result, tmp_path, number=1):
    return json.loads((tmp_path / "exports" / folder_of(result) / f"part{number}.json").read_text())


def search_requests(client):
    return [r for r in client.requests if isinstance(r, functions.messages.SearchRequest)]


async def test_search_export_writes_all_matches_of_a_long_search(client, tmp_path):
    for i in range(1, 201):
        client.add_message(client.group, i, f"meeting note {i}", sender=client.alice if i % 2 else client.me)
    result = await exporter(client, tmp_path).export_search(chat="Book club", query="meeting note")
    assert result["message_count"] == 200 and result["total_messages"] == 200
    assert not result["truncated"] and result["resume"] is None and result["part"] == 1
    assert result["chats_count"] == 1 and result["chat"]["name"] == "Book club"
    assert set(result["files"]) == {"json", "markdown"}
    # The tool result carries no message content.
    assert "messages" not in result and "meeting note 42" not in json.dumps(result)
    # The search really paginated server-side (pages of 100, newest first).
    assert [r.offset_id for r in search_requests(client)] == [0, 101, 1]
    data = part(result, tmp_path)
    assert [m["id"] for m in data["messages"]] == list(range(200, 0, -1))
    assert all(m["chat"] == {"id": -10, "name": "Book club", "username": None, "type": "group"}
               for m in data["messages"])
    assert data["search"]["kind"] == "chat" and data["search"]["query"] == "meeting note"
    assert data["search"]["chat"]["id"] == -10 and data["message_count"] == 200
    md = (tmp_path / "exports" / folder_of(result) / "part1.md").read_text()
    assert md.startswith("# Telegram search: Book club\n")
    assert "- Messages: 200 in this part, 200 in total from 1 chats" in md
    assert "**03:20:00 · Book club · Owner** (#200)\nmeeting note 200" in md


async def test_global_search_export_spans_chats(client, tmp_path):
    client.add_message(client.group, 1, "budget plan", sender=client.alice)
    client.add_message(client.news, 2, "budget news", sender=client.news)
    client.add_message(client.alice, 3, "budget talk", sender=client.alice)
    result = await exporter(client, tmp_path).export_search(query="budget")
    assert result["message_count"] == 3 and result["chats_count"] == 3 and "chat" not in result
    by_id = {m["id"]: m["chat"]["name"] for m in part(result, tmp_path)["messages"]}
    assert by_id == {3: "Alice", 2: "News", 1: "Book club"}
    assert isinstance(client.requests[-1], functions.messages.SearchGlobalRequest)
    md = (tmp_path / "exports" / folder_of(result) / "part1.md").read_text()
    assert md.startswith("# Telegram search: budget\n")
    assert "**00:02:00 · News · News** (#2)" in md


async def test_global_search_export_resumes_with_the_cursor(client, tmp_path):
    client.add_message(client.group, 1, "budget plan", sender=client.alice)
    client.add_message(client.news, 2, "budget news", sender=client.news)
    client.add_message(client.alice, 3, "budget talk", sender=client.alice)
    exp = exporter(client, tmp_path)
    first = await exp.export_search(query="budget", max_messages=2)
    assert first["truncated"] and first["truncated_reason"] == "max_messages reached"
    assert [m["id"] for m in part(first, tmp_path)["messages"]] == [3, 2]
    second = await exp.export_search(resume=first["resume"])
    assert not second["truncated"] and second["resume"] is None and second["part"] == 2
    assert [m["id"] for m in part(first, tmp_path, 2)["messages"]] == [1]
    assert second["total_messages"] == 3 and second["chats_count"] == 3


async def test_search_export_filters_the_sender_in_a_private_chat(client, tmp_path):
    for i in range(1, 6):
        client.add_message(client.alice, i, f"hello {i}", sender=client.alice if i % 2 else client.me)
    exp = exporter(client, tmp_path)
    first = await exp.export_search(chat="@alice", query="hello", sender="me", max_messages=2)
    # Telegram ignores from_id in private chats: the export keeps only the owner's messages, and
    # the cursor walks through full pages that contain no match at all.
    assert [r.offset_id for r in search_requests(client)] == [0, 4, 3]
    assert [m["id"] for m in part(first, tmp_path)["messages"]] == [4, 2]
    second = await exp.export_search(resume=first["resume"])
    assert part(first, tmp_path, 2)["messages"] == []
    assert second["total_messages"] == 2 and not second["truncated"]
    ids = [m["id"] for m in part(first, tmp_path)["messages"]] + [m["id"] for m in part(first, tmp_path, 2)["messages"]]
    assert ids == [4, 2]  # exactly the owner's messages, no duplicates, no gaps


async def test_search_export_stops_at_the_time_budget_and_resumes(client, tmp_path):
    for i in range(1, 11):
        client.add_message(client.group, i, f"topic {i}", sender=client.alice)
    ticks = iter(range(1000))
    exp = exporter(client, tmp_path, time_budget=2.5, clock=lambda: next(ticks))
    first = await exp.export_search(chat="Book club", query="topic", format="json")
    assert first["truncated"] and "time budget" in first["truncated_reason"]
    assert [m["id"] for m in part(first, tmp_path)["messages"]] == [10, 9, 8]  # stopped mid-page
    ticks = iter(range(1000))
    exp._clock = lambda: next(ticks)
    second = await exp.export_search(resume=first["resume"])
    assert second["truncated"] and second["part"] == 2
    assert [m["id"] for m in part(first, tmp_path, 2)["messages"]] == [7, 6, 5]
    exp.time_budget = 1000
    ticks = iter(range(1000))
    exp._clock = lambda: next(ticks)
    third = await exp.export_search(resume=first["resume"])
    assert not third["truncated"] and third["resume"] is None and third["part"] == 3
    ids = [m["id"] for n in (1, 2, 3) for m in part(first, tmp_path, n)["messages"]]
    assert ids == list(range(10, 0, -1))  # every part continues exactly where the last one stopped
    assert third["total_messages"] == 10 and third["chats_count"] == 1
    with pytest.raises(ReaderError, match="already complete"):
        await exp.export_search(resume=first["resume"])


async def test_search_export_caps_max_messages_and_continues(client, tmp_path):
    for i in range(1, 11):
        client.add_message(client.group, i, f"topic {i}", sender=client.alice)
    exp = exporter(client, tmp_path)
    first = await exp.export_search(chat="Book club", query="topic", max_messages=4)
    assert first["truncated"] and first["truncated_reason"] == "max_messages reached"
    assert [m["id"] for m in part(first, tmp_path)["messages"]] == [10, 9, 8, 7]
    second = await exp.export_search(resume=first["resume"])
    assert not second["truncated"]
    assert [m["id"] for m in part(first, tmp_path, 2)["messages"]] == [6, 5, 4, 3, 2, 1]
    # The exporter's own cap is above any per-call max_messages.
    capped = exporter(client, tmp_path, max_messages=4)
    result = await capped.export_search(chat="Book club", query="topic", max_messages=1000)
    assert result["message_count"] == 4 and result["truncated"]


async def test_search_export_folder_name_is_sanitized(client, tmp_path):
    client.add_message(client.group, 1, "hello", sender=client.alice)
    exp = exporter(client, tmp_path)
    result = await exp.export_search(chat="Book club", query="../../etc/passwd ?*|<>")
    name = folder_of(result)
    assert name == "search_20261018-123000_etc_passwd"
    assert "/" not in name and ".." not in name
    assert (tmp_path / "exports" / name).is_dir()
    plain = await exp.export_search(chat="Book club", format="json")
    assert folder_of(plain) == "search_20261018-123000_Book_club"


async def test_search_export_resume_is_validated(client, tmp_path):
    client.add_message(client.group, 1, "hello", sender=client.alice)
    exp = exporter(client, tmp_path)
    with pytest.raises(ReaderError, match="invalid resume"):
        await exp.export_search(resume="../evil")
    with pytest.raises(ReaderError, match="no partial search export"):
        await exp.export_search(resume="search_nope")
    chat_export = await exp.export_chat("Book club", format="json")
    with pytest.raises(ReaderError, match="no partial search export"):
        await exp.export_search(resume=Path(chat_export["directory"]).name)
    done = await exp.export_search(chat="Book club", query="hello")
    token = Path(done["directory"]).name
    with pytest.raises(ReaderError, match="no other search parameters"):
        await exp.export_search(resume=token, query="hello")


async def test_search_export_downloads_media_with_the_caps(client, tmp_path):
    client.add_message(client.alice, 1, "voice hello", sender=client.alice, media=voice())
    client.add_message(client.alice, 2, "doc hello", sender=client.alice, media=document("big.bin", 5_000_000))
    exp = exporter(client, tmp_path, media_max_bytes=1024 * 1024)
    result = await exp.export_search(chat="@alice", query="hello", include_media=True, format="json")
    assert (result["media_files"], result["media_skipped"]) == (1, 1)
    by_id = {m["id"]: m for m in part(result, tmp_path)["messages"]}
    assert by_id[1]["media"]["file"] == "media/1.oga"
    assert (tmp_path / "exports" / folder_of(result) / "media" / "1.oga").read_bytes() == b"media-of-1"
    assert by_id[2]["media"]["skipped"] == "larger than 1 MB"
    # Without include_media nothing is downloaded.
    exp._now = lambda: NOW.replace(minute=31)
    quiet = await exp.export_search(chat="@alice", query="hello", format="json")
    assert quiet["media_files"] == 0
    assert not (tmp_path / "exports" / folder_of(quiet) / "media").exists()


async def test_search_export_rejects_bad_arguments(client, tmp_path):
    exp = exporter(client, tmp_path)
    with pytest.raises(ReaderError, match="format must be"):
        await exp.export_search(chat="Book club", query="x", format="pdf")
    with pytest.raises(ReaderError, match="media must be"):
        await exp.export_search(chat="Book club", media="hologram")
    with pytest.raises(ReaderError, match="global search needs a query"):
        await exp.export_search()
    with pytest.raises(ReaderError, match="chat_type must be"):
        await exp.export_search(query="x", chat_type="bot")
    with pytest.raises(ReaderError, match="sender and topic_id need a specific chat"):
        await exp.export_search(query="x", sender="@alice")
    with pytest.raises(ReaderError, match="chat_type applies only to a global search"):
        await exp.export_search(chat="Book club", query="x", chat_type="group")


async def test_search_export_requests_stay_on_the_guard_allow_list(client, tmp_path):
    for i in range(1, 6):
        client.add_message(client.group, i, f"hello {i}", sender=client.alice)
        client.add_message(client.news, i, f"hello news {i}", sender=client.news)
    exp = exporter(client, tmp_path)
    await exp.export_search(chat="Book club", query="hello")
    await exp.export_search(query="hello", max_messages=3)
    allowed = {functions.messages.SearchRequest, functions.messages.SearchGlobalRequest,
               functions.account.GetNotifySettingsRequest}
    # Every raw request goes through the guard (the fake client enforces it); assert we stayed
    # within the search part of the allow-list and nothing else was even attempted.
    assert {type(r) for r in client.requests} <= allowed
    assert {type(r) for r in client.requests} & {functions.messages.SearchRequest,
                                                functions.messages.SearchGlobalRequest}
