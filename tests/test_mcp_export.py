"""Export of chats to JSON and Markdown files."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.mcp_fakes import FakeTelegramClient, basic_group, document, user, voice
from transcription_bot.mcp_server.export import Exporter
from transcription_bot.mcp_server.reader import ReaderError, TelegramReader

NOW = datetime(2026, 10, 18, 12, 30, tzinfo=timezone.utc)


@pytest.fixture
def client():
    c = FakeTelegramClient()
    c.alice = c.add_dialog(user(1, "Alice", username="alice"))
    c.group = c.add_dialog(basic_group(10, "Book club / 2026"))
    for i in range(1, 11):
        c.add_message(c.group, i, f"line {i}\nsecond line" + " long" * 2000 * (i == 5), sender=c.alice if i % 2 else c.me,
                      minutes=i * 60)
    return c


def exporter(client, tmp_path, **kwargs):
    return Exporter(TelegramReader(client), tmp_path / "exports", Path("/home/owner/exports"), now=lambda: NOW,
                    **kwargs)


async def test_export_writes_json_and_markdown(client, tmp_path):
    (tmp_path / "exports").mkdir()
    result = await exporter(client, tmp_path).export_chat("Book club / 2026")
    folder = "Book_club_2026_10_20261018-123000"
    assert result["message_count"] == 10 and not result["truncated"]
    assert result["directory"] == f"/home/owner/exports/{folder}"
    assert result["files"] == {"json": f"/home/owner/exports/{folder}/messages.json",
                               "markdown": f"/home/owner/exports/{folder}/messages.md"}
    data = json.loads((tmp_path / "exports" / folder / "messages.json").read_text())
    assert data["chat"]["name"] == "Book club / 2026" and data["message_count"] == 10
    assert [m["id"] for m in data["messages"]] == list(range(1, 11))  # oldest first
    assert len(data["messages"][4]["text"]) > 10_000 and "text_truncated" not in data["messages"][4]  # full text
    md = (tmp_path / "exports" / folder / "messages.md").read_text()
    assert md.startswith("# Book club / 2026\n")
    assert "- Messages: 10" in md and "## 2026-01-01" in md
    assert "**01:00:00 · Alice** (#1)\nline 1\nsecond line" in md
    assert "**02:00:00 · Owner** (#2)" in md


async def test_export_date_range_formats_and_limits(client, tmp_path):
    exp = exporter(client, tmp_path)
    ranged = await exp.export_chat(-10, since="2026-01-01T03:00", until="2026-01-01T06:00", format="json")
    assert ranged["message_count"] == 3 and set(ranged["files"]) == {"json"}
    data = json.loads(Path(tmp_path / "exports" / Path(ranged["directory"]).name / "messages.json").read_text())
    assert [m["id"] for m in data["messages"]] == [3, 4, 5]
    assert data["since"] == "2026-01-01T03:00:00+00:00" and data["until"] == "2026-01-01T06:00:00+00:00"

    exp._now = lambda: NOW.replace(minute=31)
    limited = await exp.export_chat(-10, format="markdown", max_messages=4)
    assert limited["message_count"] == 4 and limited["truncated"] and set(limited["files"]) == {"markdown"}
    md = (tmp_path / "exports" / Path(limited["directory"]).name / "messages.md").read_text()
    assert "(limit reached, export is incomplete)" in md

    capped = exporter(client, tmp_path, max_messages=2)
    capped._now = lambda: NOW.replace(minute=32)
    assert (await capped.export_chat(-10, max_messages=1000))["message_count"] == 2
    with pytest.raises(ReaderError, match="format"):
        await exp.export_chat(-10, format="pdf")


async def test_export_with_media(client, tmp_path):
    client.add_message(client.alice, 1, "voice", sender=client.alice, media=voice())
    client.add_message(client.alice, 2, "huge", sender=client.alice, media=document("movie.mkv", 500 * 1024 * 1024))
    client.add_message(client.alice, 3, "text only", sender=client.me)
    result = await exporter(client, tmp_path, media_max_bytes=1024 * 1024).export_chat("@alice", include_media=True)
    assert (result["media_files"], result["media_skipped"]) == (1, 1)
    folder = tmp_path / "exports" / Path(result["directory"]).name
    data = json.loads((folder / "messages.json").read_text())
    by_id = {m["id"]: m for m in data["messages"]}
    assert by_id[1]["media"]["file"] == "media/1.oga"
    assert (folder / "media" / "1.oga").read_bytes() == b"media-of-1"
    assert by_id[2]["media"]["skipped"] == "larger than 1 MB" and not (folder / "media" / "2.mkv").exists()
    assert "_[voice: [media/1.oga](media/1.oga)]_" in (folder / "messages.md").read_text()


async def test_export_without_media_downloads_nothing(client, tmp_path):
    client.add_message(client.alice, 1, "voice", sender=client.alice, media=voice())
    result = await exporter(client, tmp_path).export_chat("@alice")
    assert result["media_files"] == 0
    assert not (tmp_path / "exports" / Path(result["directory"]).name / "media").exists()


async def test_export_into_unwritable_directory_fails_clearly(client, tmp_path):
    blocker = tmp_path / "exports"
    blocker.write_text("not a directory")
    with pytest.raises(ReaderError, match="must exist and be writable"):
        await exporter(client, tmp_path).export_chat("@alice")


async def test_exports_in_the_same_second_get_separate_folders(client, tmp_path):
    exp = exporter(client, tmp_path)
    first = await exp.export_chat("@alice")
    second = await exp.export_chat("@alice")
    assert second["directory"] == first["directory"] + "-2"
    assert Path(tmp_path / "exports" / Path(second["directory"]).name / "messages.json").exists()
