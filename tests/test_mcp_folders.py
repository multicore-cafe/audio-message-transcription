"""Folder (dialog filter) rules and the folder tools."""

import pytest
from telethon import types, utils

from tests.mcp_fakes import ME_ID, FakeTelegramClient, basic_group, channel, user
from transcription_bot.mcp_server.folders import DialogFacts, Folder, parse_filter
from transcription_bot.mcp_server.reader import ReaderError, TelegramReader


def facts(peer_id=1, kind="user", *, contact=False, muted=False, unread=False, mentions=False, archived=False):
    return DialogFacts(peer_id=peer_id, kind=kind, is_contact=contact, muted=muted, unread=unread,
                       unread_mentions=mentions, archived=archived)


def folder(**flags):
    return Folder(id=2, title="F", kind="filter", emoticon=None, pinned=flags.pop("pinned", ()),
                  include=flags.pop("include", ()), exclude=flags.pop("exclude", ()), **flags)


@pytest.mark.parametrize(("flag", "dialog", "expected"), [
    ("contacts", facts(contact=True), True),
    ("contacts", facts(contact=False), False),
    ("non_contacts", facts(contact=False), True),
    ("non_contacts", facts(contact=True), False),
    ("bots", facts(kind="bot"), True),
    ("contacts", facts(kind="bot", contact=True), False),  # bots only match the bots flag
    ("groups", facts(kind="group"), True),
    ("groups", facts(kind="channel"), False),
    ("broadcasts", facts(kind="channel"), True),
    ("broadcasts", facts(kind="group"), False),
])
def test_category_flags(flag, dialog, expected):
    assert folder(**{flag: True}).contains(dialog) is expected


def test_no_flags_and_no_peers_matches_nothing():
    assert not folder().contains(facts(contact=True))


def test_included_and_pinned_peers_win_over_everything():
    rules = folder(include=(5,), pinned=(6,), exclude_muted=True, exclude_read=True, exclude_archived=True)
    assert rules.contains(facts(5, "channel", muted=True, archived=True))
    assert rules.contains(facts(6, "bot", muted=True))


def test_excluded_peers_win_over_categories():
    rules = folder(groups=True, exclude=(7,))
    assert not rules.contains(facts(7, "group"))
    assert rules.contains(facts(8, "group"))


def test_exclude_muted_read_and_archived():
    assert not folder(groups=True, exclude_muted=True).contains(facts(kind="group", muted=True, unread=True))
    assert not folder(groups=True, exclude_read=True).contains(facts(kind="group", unread=False))
    assert folder(groups=True, exclude_read=True).contains(facts(kind="group", unread=True))
    assert not folder(groups=True, exclude_archived=True).contains(facts(kind="group", archived=True))
    assert folder(groups=True).contains(facts(kind="group", archived=True))


def test_unread_mentions_keep_muted_or_read_chats_visible():
    rules = folder(groups=True, exclude_muted=True, exclude_read=True)
    assert rules.contains(facts(kind="group", muted=True, mentions=True))
    # ... but not archived ones when archived chats are excluded.
    assert not folder(groups=True, exclude_archived=True).contains(facts(kind="group", mentions=True, archived=True))


def test_default_folder_is_all_unarchived_chats():
    default = parse_filter(types.DialogFilterDefault(), ME_ID)
    assert default.contains(facts()) and not default.contains(facts(archived=True))


def test_parse_filter_handles_titles_self_and_chatlists():
    raw = types.DialogFilter(
        id=3, title=types.TextWithEntities("Work", []), pinned_peers=[types.InputPeerSelf()],
        include_peers=[types.InputPeerChannel(50, 1)], exclude_peers=[types.InputPeerChat(9)],
        groups=True, exclude_read=True, emoticon="💼",
    )
    parsed = parse_filter(raw, ME_ID)
    assert (parsed.title, parsed.pinned, parsed.include, parsed.exclude) == ("Work", (ME_ID,), (-1000000000050,), (-9,))
    assert parsed.flags()["groups"] and parsed.flags()["exclude_read"] and not parsed.flags()["bots"]
    shared = parse_filter(types.DialogFilterChatlist(id=4, title=types.TextWithEntities("Shared", []), pinned_peers=[],
                                                     include_peers=[types.InputPeerUser(1, 7)]), ME_ID)
    assert shared.kind == "chatlist" and shared.contains(facts(1)) and not shared.contains(facts(2))


@pytest.fixture
def account():
    client = FakeTelegramClient()
    alice = client.add_dialog(user(1, "Alice", contact=True), unread=1)
    bob = client.add_dialog(user(2, "Bob"))
    bot = client.add_dialog(user(3, "Helper", bot=True))
    family = client.add_dialog(basic_group(10, "Family"), muted=True)
    work = client.add_dialog(channel(20, "Work chat", megagroup=True), unread=4)
    noisy = client.add_dialog(channel(21, "Noisy group", megagroup=True), muted=True, mentions=1, unread=9)
    news = client.add_dialog(channel(30, "News"), archived=True, unread=3)
    old = client.add_dialog(channel(31, "Old channel"), archived=True)
    for i, entity in enumerate([alice, bob, bot, family, work, noisy, news, old], start=1):
        client.add_message(entity, i, f"message {i}")
    peer = utils.get_input_peer
    client.filters = [
        types.DialogFilterDefault(),
        types.DialogFilter(
            id=2, title=types.TextWithEntities("Groups", []), pinned_peers=[peer(work)],
            include_peers=[peer(alice)], exclude_peers=[peer(family)],
            groups=True, exclude_muted=True, exclude_archived=True,
        ),
        types.DialogFilter(
            id=3, title=types.TextWithEntities("Unread channels", []), pinned_peers=[], include_peers=[],
            exclude_peers=[], broadcasts=True, exclude_read=True,
        ),
        types.DialogFilter(
            id=4, title=types.TextWithEntities("People", []), pinned_peers=[], include_peers=[],
            exclude_peers=[peer(bob)], contacts=True, non_contacts=True, bots=True,
        ),
    ]
    return client


async def test_list_folders(account):
    result = await TelegramReader(account).list_folders()
    folders = {f["id"]: f for f in result["folders"]}
    assert folders[0] == {"id": 0, "title": "All chats", "kind": "default"}
    groups = folders[2]
    assert groups["title"] == "Groups" and groups["flags"]["groups"] and groups["flags"]["exclude_muted"]
    assert groups["pinned_peers"] == [{"id": -1000000000020, "name": "Work chat"}]
    assert groups["include_peers"] == [{"id": 1, "name": "Alice"}]
    assert groups["exclude_peers"] == [{"id": -10, "name": "Family"}]


async def chat_names(client, folder_id):
    result = await TelegramReader(client).list_folder_chats(folder_id)
    return [c["name"] for c in result["chats"]]


async def test_folder_chats_follow_the_rules(account):
    # Pinned first; Alice is included explicitly; Family is excluded; Noisy is muted but has a mention.
    assert await chat_names(account, 2) == ["Work chat", "Noisy group", "Alice"]
    assert await chat_names(account, 3) == ["News"]  # Old channel has nothing unread
    assert await chat_names(account, 4) == ["Helper", "Alice"]
    assert set(await chat_names(account, 0)) == {"Alice", "Bob", "Helper", "Family", "Work chat", "Noisy group"}


async def test_folder_chats_pagination_and_unknown_folder(account):
    reader = TelegramReader(account)
    page = await reader.list_folder_chats(2, limit=2)
    assert [c["name"] for c in page["chats"]] == ["Work chat", "Noisy group"]
    assert (page["total"], page["next_offset"]) == (3, 2)
    rest = await reader.list_folder_chats(2, limit=2, offset=page["next_offset"])
    assert [c["name"] for c in rest["chats"]] == ["Alice"] and rest["next_offset"] is None
    with pytest.raises(ReaderError, match="no folder"):
        await reader.list_folder_chats(99)
