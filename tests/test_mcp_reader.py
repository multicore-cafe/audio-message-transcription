"""Reading tools against an in-memory account."""

from datetime import datetime, timezone

import pytest
from telethon import functions, types

from tests.mcp_fakes import BASE_DATE, ME_ID, FakeTelegramClient, basic_group, channel, document, user, voice
from transcription_bot.mcp_server.reader import MAX_LIMIT, ReaderError, TelegramReader, parse_date


@pytest.fixture
def client():
    c = FakeTelegramClient()
    c.alice = c.add_dialog(user(1, "Alice", "Smith", username="alice", contact=True), unread=2)
    c.bob = c.add_dialog(user(2, "Bob"), archived=True)
    c.bot = c.add_dialog(user(3, "Helper", bot=True))
    c.family = c.add_dialog(basic_group(10, "Family"), mentions=1)
    c.news = c.add_dialog(channel(30, "News", username="news_channel", members=1200), marked=True)
    c.forum = c.add_dialog(channel(40, "Forum", megagroup=True, forum=True))
    c.public = c.add(channel(60, "Public", username="public_chan"))  # not in the dialog list
    for i in range(1, 121):  # 120 messages in Family, one per minute
        c.add_message(c.family, i, f"family message {i}", sender=c.alice if i % 2 else c.me)
    return c


@pytest.fixture
def reader(client):
    return TelegramReader(client)


async def test_list_dialogs_filters(reader):
    everything = await reader.list_dialogs()
    assert everything["total"] == 6
    assert {d["name"] for d in everything["dialogs"]} == {"Alice Smith", "Bob", "Helper", "Family", "News", "Forum"}
    assert [d["name"] for d in (await reader.list_dialogs(chat_type="bot"))["dialogs"]] == ["Helper"]
    assert {d["name"] for d in (await reader.list_dialogs(chat_type="group"))["dialogs"]} == {"Family", "Forum"}
    assert [d["name"] for d in (await reader.list_dialogs(chat_type="channel"))["dialogs"]] == ["News"]
    assert [d["name"] for d in (await reader.list_dialogs(archived=True))["dialogs"]] == ["Bob"]
    unread = {d["name"]: d for d in (await reader.list_dialogs(unread_only=True))["dialogs"]}
    assert set(unread) == {"Alice Smith", "Family", "News"}
    assert unread["Alice Smith"]["unread_count"] == 2
    assert unread["Family"]["unread_mentions_count"] == 1
    assert unread["News"]["marked_unread"] is True
    assert [d["name"] for d in (await reader.list_dialogs(name_contains="SMITH"))["dialogs"]] == ["Alice Smith"]
    assert [d["name"] for d in (await reader.list_dialogs(name_contains="news_ch"))["dialogs"]] == ["News"]
    with pytest.raises(ReaderError):
        await reader.list_dialogs(chat_type="robot")


async def test_list_dialogs_pagination_and_cap(reader, client):
    first = await reader.list_dialogs(limit=4)
    second = await reader.list_dialogs(limit=4, offset=first["next_offset"])
    assert len(first["dialogs"]) == 4 and first["next_offset"] == 4
    assert len(second["dialogs"]) == 2 and second["next_offset"] is None
    assert {d["id"] for d in first["dialogs"]}.isdisjoint({d["id"] for d in second["dialogs"]})
    for i in range(600):
        client.add_dialog(user(10_000 + i, f"User {i}"))
    reader.dialog_cache_seconds = -1  # reload
    assert len((await reader.list_dialogs(limit=100_000))["dialogs"]) == 500


async def test_dialogs_are_cached(client):
    now = [0.0]
    reader = TelegramReader(client, dialog_cache_seconds=60, clock=lambda: now[0])
    await reader.list_dialogs()
    await reader.resolve("Family")
    assert client.dialog_loads == 1
    now[0] = 61
    await reader.list_dialogs()
    assert client.dialog_loads == 2


async def test_resolve(reader, client):
    assert (await reader.resolve(-10)).title == "Family"
    assert (await reader.resolve("-1000000000030")).title == "News"
    assert (await reader.resolve("@news_channel")).title == "News"
    assert (await reader.resolve("https://t.me/news_channel")).title == "News"
    assert (await reader.resolve("Family")).title == "Family"
    assert (await reader.resolve("family")).title == "Family"
    assert (await reader.resolve("me")).id == ME_ID
    assert (await reader.resolve("@public_chan")).title == "Public"  # contacts.resolveUsername
    assert [type(r).__name__ for r in client.requests] == ["ResolveUsernameRequest"]
    for bad, message in [("https://t.me/+AbCdEf", "invite links"), ("t.me/joinchat/AbCd", "invite links"),
                         ("No such chat at all", "no chat"), ("-555", "not among"), ("", "empty")]:
        with pytest.raises(ReaderError, match=message):
            await reader.resolve(bad)
    client.add_dialog(basic_group(11, "Family"))
    reader.dialog_cache_seconds = -1
    with pytest.raises(ReaderError, match="several chats"):
        await reader.resolve("Family")


async def test_read_messages_pages_through_the_chat_without_gaps(reader):
    page = await reader.read_messages("Family", limit=50)
    assert [m["id"] for m in page["messages"]] == list(range(120, 70, -1))
    assert page["has_more"] and page["next_offset_id"] == 71
    seen = [m["id"] for m in page["messages"]]
    while page["has_more"]:
        page = await reader.read_messages("Family", limit=50, offset_id=page["next_offset_id"])
        seen += [m["id"] for m in page["messages"]]
    assert seen == list(range(120, 0, -1))
    assert page["next_offset_id"] is None


async def test_read_messages_oldest_first_and_date_range(reader):
    page = await reader.read_messages("Family", oldest_first=True, limit=5)
    assert [m["id"] for m in page["messages"]] == [1, 2, 3, 4, 5]
    page = await reader.read_messages("Family", oldest_first=True, limit=5, offset_id=page["next_offset_id"])
    assert [m["id"] for m in page["messages"]] == [6, 7, 8, 9, 10]
    # Message i is sent i minutes after midnight.
    window = await reader.read_messages("Family", since="2026-01-01T00:30", until="2026-01-01T00:40")
    assert [m["id"] for m in window["messages"]] == list(range(39, 29, -1)) and not window["has_more"]
    window = await reader.read_messages("Family", since="2026-01-01T00:30", until="2026-01-01T00:40",
                                        oldest_first=True)
    assert [m["id"] for m in window["messages"]] == list(range(30, 40)) and not window["has_more"]
    assert (await reader.read_messages("Family", since="2026-01-02"))["messages"] == []
    assert len((await reader.read_messages("Family", until="2026-01-01"))["messages"]) == 50  # whole day


async def test_read_messages_limits_and_message_fields(reader, client):
    assert len((await reader.read_messages("Family", limit=10_000))["messages"]) == MAX_LIMIT
    message = (await reader.read_messages("Family", limit=1))["messages"][0]
    assert message == {"id": 120, "chat_id": -10, "date": "2026-01-01T02:00:00+00:00", "out": True,
                       "sender": {"id": ME_ID, "name": "Owner", "username": "owner", "type": "user"},
                       "text": "family message 120"}
    client.add_message(client.alice, 500, "x" * 10_000, sender=client.alice)
    cut = (await reader.read_messages("Alice Smith", limit=1, max_text=100))["messages"][0]
    assert len(cut["text"]) == 100 and cut["text_truncated"] == 10_000


async def test_read_messages_respects_the_response_size_budget(client):
    for i in range(1, 31):
        client.add_message(client.news, i, "y" * 4000, sender=client.news)
    reader = TelegramReader(client, max_response_chars=20_000)
    page = await reader.read_messages("News", limit=30)
    assert 1 <= len(page["messages"]) < 30
    assert page["truncated_by_size"] and page["has_more"]
    assert page["next_offset_id"] == page["messages"][-1]["id"]


async def test_read_messages_of_a_topic_and_replies(reader, client):
    client.add_message(client.forum, 1, "topic created", action=types.MessageActionTopicCreate("Ideas", 0))
    client.add_message(client.forum, 2, "first idea", sender=client.alice, topic=1)
    client.add_message(client.forum, 3, "other topic", sender=client.alice, topic=99)
    client.add_message(client.forum, 4, "reply to idea", sender=client.me, topic=1, reply_to=2)
    topic = await reader.read_messages("Forum", topic_id=1)
    assert [m["id"] for m in topic["messages"]] == [4, 2]
    assert topic["messages"][0]["reply_to"] == {"message_id": 2, "top_id": 1, "forum_topic": True}
    assert topic["messages"][0]["topic_id"] == 1
    replies = await reader.read_messages("Forum", reply_to=2)
    assert [m["id"] for m in replies["messages"]] == [4]
    with pytest.raises(ReaderError):
        await reader.read_messages("Forum", topic_id=1, reply_to=2)


async def test_message_details(reader, client):
    client.add_message(client.alice, 900, "voice note", sender=client.alice, media=voice(7), reactions=types.MessageReactions(
        results=[types.ReactionCount(types.ReactionEmoji("👍"), 2), types.ReactionCount(types.ReactionCustomEmoji(5), 1)]))
    client.add_message(client.alice, 901, "fwd", sender=client.alice, fwd_from=types.MessageFwdHeader(
        date=BASE_DATE, from_id=types.PeerChannel(30), channel_post=7))
    client.add_message(client.alice, 902, "", sender=client.me, media=document("report.pdf", 2048, "application/pdf"))
    result = await reader.get_messages("@alice", [900, 901, 902, 903])
    voice_msg, forwarded, doc = result["messages"]
    assert voice_msg["media"] == {"type": "voice", "file_name": "voice.ogg", "mime_type": "audio/ogg", "size": 100,
                                  "duration": 7}
    assert voice_msg["reactions"] == [{"reaction": "👍", "count": 2}, {"reaction": "custom:5", "count": 1}]
    assert forwarded["forward"]["from"]["name"] == "News" and forwarded["forward"]["channel_post"] == 7
    assert doc["media"]["type"] == "document" and doc["media"]["file_name"] == "report.pdf" and doc["out"]
    assert result["missing_ids"] == [903]
    with pytest.raises(ReaderError):
        await reader.get_messages("@alice", list(range(MAX_LIMIT + 1)))


async def test_search_in_a_chat(reader, client):
    client.add_message(client.alice, 200, "hello world", sender=client.alice, minutes=24 * 60)
    client.add_message(client.alice, 201, "another hello", sender=client.me, minutes=24 * 60 + 5)
    client.add_message(client.alice, 202, "hello as voice", sender=client.alice, media=voice(), minutes=48 * 60)
    found = await reader.search_messages("Alice Smith", query="hello")
    assert [m["id"] for m in found["messages"]] == [202, 201, 200] and not found["has_more"]
    request = client.requests[-1]
    assert isinstance(request, functions.messages.SearchRequest) and request.q == "hello"
    assert [m["id"] for m in (await reader.search_messages("Alice Smith", query="hello", media="voice"))["messages"]] \
        == [202]
    by_date = await reader.search_messages("Alice Smith", query="hello", since="2026-01-02", until="2026-01-02")
    assert [m["id"] for m in by_date["messages"]] == [201, 200]
    assert client.requests[-1].min_date == datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert client.requests[-1].max_date == datetime(2026, 1, 3, tzinfo=timezone.utc)
    # Telegram ignores from_id in private chats, so the sender is also checked locally.
    from_me = await reader.search_messages("Alice Smith", query="hello", sender="me")
    assert [m["id"] for m in from_me["messages"]] == [201]
    page = await reader.search_messages("Alice Smith", query="hello", limit=2)
    assert page["has_more"] and page["next_offset_id"] == 201
    rest = await reader.search_messages("Alice Smith", query="hello", limit=2, offset_id=page["next_offset_id"])
    assert [m["id"] for m in rest["messages"]] == [200]
    with pytest.raises(ReaderError, match="media must be"):
        await reader.search_messages("Alice Smith", media="hologram")


async def test_search_in_a_group_by_sender_and_topic(reader, client):
    from_alice = await reader.search_messages("Family", query="family message 11", sender="@alice")
    assert {m["id"] for m in from_alice["messages"]} == {111, 119, 117, 115, 113, 11}
    assert all(m["sender"]["name"] == "Alice Smith" for m in from_alice["messages"])
    client.add_message(client.forum, 2, "idea one", sender=client.alice, topic=1)
    client.add_message(client.forum, 3, "idea two", sender=client.alice, topic=5)
    in_topic = await reader.search_messages("Forum", query="idea", topic_id=5)
    assert [m["id"] for m in in_topic["messages"]] == [3]
    assert client.requests[-1].top_msg_id == 5


async def test_global_search_with_cursor(reader, client):
    client.add_message(client.news, 300, "budget news", sender=client.news, minutes=1000)
    client.add_message(client.alice, 301, "about the budget", sender=client.alice, minutes=1001)
    client.add_message(client.family, 302, "budget again", sender=client.me, minutes=1002)
    first = await reader.search_global("budget", limit=2)
    assert [(m["id"], m["chat"]["name"]) for m in first["messages"]] == [(302, "Family"), (301, "Alice Smith")]
    assert first["next_cursor"]
    second = await reader.search_global("budget", limit=2, cursor=first["next_cursor"])
    assert [m["id"] for m in second["messages"]] == [300] and second["next_cursor"] is None
    assert client.requests[-1].broadcasts_only is None
    await reader.search_global("budget", chat_type="channel")
    assert client.requests[-1].broadcasts_only is True
    with pytest.raises(ReaderError):
        await reader.search_global("  ")


async def test_chat_info(reader, client):
    discussion = client.add(channel(31, "News chat", megagroup=True))
    client.full[-1000000000030] = types.ChannelFull(
        id=30, about="Daily news", read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0,
        chat_photo=types.PhotoEmpty(0), notify_settings=types.PeerNotifySettings(), bot_info=[], pts=1,
        participants_count=1234, linked_chat_id=discussion.id,
    )
    info = await reader.chat_info("@news_channel")
    assert info["type"] == "channel" and info["about"] == "Daily news" and info["members_count"] == 1234
    assert info["linked_chat"] == {"id": -1000000000031, "name": "News chat", "username": None, "type": "group"}
    assert info["forum"] is False and info["dialog"]["marked_unread"] is True
    client.full[1] = types.UserFull(id=1, settings=types.PeerSettings(), notify_settings=types.PeerNotifySettings(),
                                    common_chats_count=3, about="Hi, I'm Alice")
    alice = await reader.chat_info("Alice Smith")
    assert alice["about"] == "Hi, I'm Alice" and alice["contact"] is True and alice["common_chats_count"] == 3
    client.full[-10] = types.ChatFull(id=10, about="", participants=types.ChatParticipants(10, [
        types.ChatParticipant(1, 0, BASE_DATE), types.ChatParticipant(ME_ID, 0, BASE_DATE)], 1),
        notify_settings=types.PeerNotifySettings())
    assert (await reader.chat_info("Family"))["members_count"] == 2


async def test_forum_topics(reader, client):
    client.topics[-1000000000040] = [
        types.ForumTopic(id=i, date=BASE_DATE, peer=types.PeerChannel(40), title=f"Topic {i}", icon_color=0,
                         top_message=i * 10, read_inbox_max_id=0, read_outbox_max_id=0, unread_count=i,
                         unread_mentions_count=0, unread_reactions_count=0, unread_poll_votes_count=0,
                         from_id=types.PeerUser(1), notify_settings=types.PeerNotifySettings(), closed=i == 3)
        for i in (5, 4, 3, 2, 1)
    ]
    page = await reader.list_forum_topics("Forum", limit=3)
    assert [t["id"] for t in page["topics"]] == [5, 4, 3] and page["count"] == 5
    assert page["topics"][2]["closed"] is True and page["topics"][0]["unread_count"] == 5
    rest = await reader.list_forum_topics("Forum", limit=3, cursor=page["next_cursor"])
    assert [t["id"] for t in rest["topics"]] == [2, 1] and rest["next_cursor"] is None
    with pytest.raises(ReaderError, match="not a forum"):
        await reader.list_forum_topics("Family")


def test_parse_date():
    assert parse_date("2026-03-01") == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert parse_date("2026-03-01", end=True) == datetime(2026, 3, 2, tzinfo=timezone.utc)
    assert parse_date("2026-03-01T10:00:00+02:00") == datetime(2026, 3, 1, 8, tzinfo=timezone.utc)
    assert parse_date("2026-03-01T10:00Z") == datetime(2026, 3, 1, 10, tzinfo=timezone.utc)
    assert parse_date(None) is None and parse_date("") is None
    with pytest.raises(ReaderError):
        parse_date("yesterday")
