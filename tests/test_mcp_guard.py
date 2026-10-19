"""The read-only guard: forbidden requests never reach the network, the ones the tools use do."""

import asyncio
import inspect
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import telethon
from telethon import functions, types
from telethon.client.telegrambaseclient import LAYER
from telethon.network import MTProtoSender
from telethon.tl.tlobject import TLRequest

from tests.mcp_fakes import channel, user
from transcription_bot.mcp_server.export import Exporter
from transcription_bot.mcp_server.guard import (
    LOGIN_REQUESTS,
    READ_ONLY_REQUESTS,
    SAVED_MESSAGES_REQUESTS,
    WRAPPERS,
    GuardedMTProtoSender,
    ReadOnlyTelegramClient,
    ReadOnlyViolation,
    check_request,
)
from transcription_bot.mcp_server.reader import TelegramReader
from transcription_bot.mcp_server.saved import SavedMessagesWriter

PEER = types.InputPeerUser(1, 7)
CHANNEL = types.InputChannel(50, 3)
CHANNEL_PEER = types.InputPeerChannel(50, 3)

# The requests named in the specification, plus other ways to change the account.
FORBIDDEN = {
    "messages.ReadHistory": functions.messages.ReadHistoryRequest(PEER, 10),
    "channels.ReadHistory": functions.channels.ReadHistoryRequest(CHANNEL, 10),
    "messages.ReadMentions": functions.messages.ReadMentionsRequest(PEER),
    "messages.ReadReactions": functions.messages.ReadReactionsRequest(PEER),
    "messages.ReadMessageContents": functions.messages.ReadMessageContentsRequest([1]),
    "channels.ReadMessageContents": functions.channels.ReadMessageContentsRequest(CHANNEL, [1]),
    "messages.ReadDiscussion": functions.messages.ReadDiscussionRequest(CHANNEL_PEER, 1, 1),
    "messages.GetMessagesViews": functions.messages.GetMessagesViewsRequest(CHANNEL_PEER, [1], increment=True),
    "messages.GetMessagesViews(no increment)": functions.messages.GetMessagesViewsRequest(CHANNEL_PEER, [1], False),
    "messages.MarkDialogUnread": functions.messages.MarkDialogUnreadRequest(types.InputDialogPeer(PEER), False),
    "account.UpdateStatus": functions.account.UpdateStatusRequest(offline=False),
    "messages.SendMessage": functions.messages.SendMessageRequest(PEER, "hi"),
    "messages.SendMedia": functions.messages.SendMediaRequest(PEER, types.InputMediaEmpty(), ""),
    "messages.ForwardMessages": functions.messages.ForwardMessagesRequest(PEER, [1], PEER),
    "messages.SendReaction": functions.messages.SendReactionRequest(PEER, 1),
    "messages.SetTyping": functions.messages.SetTypingRequest(PEER, types.SendMessageTypingAction()),
    "messages.EditMessage": functions.messages.EditMessageRequest(PEER, 1, message="x"),
    "messages.DeleteMessages": functions.messages.DeleteMessagesRequest([1]),
    "channels.DeleteMessages": functions.channels.DeleteMessagesRequest(CHANNEL, [1]),
    "messages.DeleteHistory": functions.messages.DeleteHistoryRequest(PEER, 0),
    "channels.JoinChannel": functions.channels.JoinChannelRequest(CHANNEL),
    "channels.LeaveChannel": functions.channels.LeaveChannelRequest(CHANNEL),
    "messages.ImportChatInvite": functions.messages.ImportChatInviteRequest("hash"),
    "messages.CheckChatInvite": functions.messages.CheckChatInviteRequest("hash"),
    "messages.DeleteChatUser": functions.messages.DeleteChatUserRequest(1, types.InputUserSelf()),
    "upload.SaveFilePart": functions.upload.SaveFilePartRequest(1, 0, b""),
    "upload.SaveBigFilePart": functions.upload.SaveBigFilePartRequest(1, 0, 1, b""),
    "folders.EditPeerFolders": functions.folders.EditPeerFoldersRequest([]),
    "messages.UpdateDialogFilter": functions.messages.UpdateDialogFilterRequest(1),
    "stories.ReadStories": functions.stories.ReadStoriesRequest(PEER, 1),
    "stories.IncrementStoryViews": functions.stories.IncrementStoryViewsRequest(PEER, [1]),
    "messages.ViewSponsoredMessage": functions.messages.ViewSponsoredMessageRequest(b"x"),
    "auth.LogOut": functions.auth.LogOutRequest(),
    "auth.SendCode (outside login)": functions.auth.SendCodeRequest("+1", 1, "x", types.CodeSettings()),
    "account.UpdateProfile": functions.account.UpdateProfileRequest(about="x"),
    "account.UpdateNotifySettings": functions.account.UpdateNotifySettingsRequest(
        types.InputNotifyUsers(), types.InputPeerNotifySettings(mute_until=None)),
    "account.ResetNotifySettings": functions.account.ResetNotifySettingsRequest(),
    "contacts.Block": functions.contacts.BlockRequest(PEER),
}

# Read-only requests the tools and Telethon's own machinery send.
ALLOWED = {
    "help.GetConfig": functions.help.GetConfigRequest(),
    "updates.GetState": functions.updates.GetStateRequest(),
    "users.GetUsers": functions.users.GetUsersRequest([types.InputUserSelf()]),
    "users.GetFullUser": functions.users.GetFullUserRequest(types.InputUserSelf()),
    "contacts.ResolveUsername": functions.contacts.ResolveUsernameRequest("durov"),
    "channels.GetFullChannel": functions.channels.GetFullChannelRequest(CHANNEL),
    "messages.GetFullChat": functions.messages.GetFullChatRequest(1),
    "messages.GetDialogs": functions.messages.GetDialogsRequest(None, 0, types.InputPeerEmpty(), 100, 0),
    "messages.GetDialogFilters": functions.messages.GetDialogFiltersRequest(),
    "messages.GetForumTopics": functions.messages.GetForumTopicsRequest(CHANNEL_PEER, None, 0, 0, 10),
    "messages.GetHistory": functions.messages.GetHistoryRequest(PEER, 0, None, 0, 10, 0, 0, 0),
    "messages.GetReplies": functions.messages.GetRepliesRequest(CHANNEL_PEER, 1, 0, None, 0, 10, 0, 0, 0),
    "channels.GetMessages": functions.channels.GetMessagesRequest(CHANNEL, [types.InputMessageID(1)]),
    "messages.Search": functions.messages.SearchRequest(
        PEER, "q", types.InputMessagesFilterEmpty(), None, None, 0, 0, 10, 0, 0, 0),
    "messages.SearchGlobal": functions.messages.SearchGlobalRequest(
        "q", types.InputMessagesFilterEmpty(), None, None, 0, types.InputPeerEmpty(), 0, 10),
    "upload.GetFile": functions.upload.GetFileRequest(types.InputDocumentFileLocation(1, 1, b"", ""), 0, 4096),
    "account.GetNotifySettings(users)": functions.account.GetNotifySettingsRequest(types.InputNotifyUsers()),
    "account.GetNotifySettings(chats)": functions.account.GetNotifySettingsRequest(types.InputNotifyChats()),
    "account.GetNotifySettings(broadcasts)": functions.account.GetNotifySettingsRequest(types.InputNotifyBroadcasts()),
}


def init_connection(query):
    return functions.InitConnectionRequest(1, "test", "1", "1", "en", "", "en", query)


@pytest.mark.parametrize("request_", FORBIDDEN.values(), ids=FORBIDDEN.keys())
def test_forbidden_requests_are_rejected(request_):
    with pytest.raises(ReadOnlyViolation):
        check_request(request_)


@pytest.mark.parametrize("request_", ALLOWED.values(), ids=ALLOWED.keys())
def test_read_requests_are_allowed(request_):
    check_request(request_)


@pytest.mark.parametrize("request_", FORBIDDEN.values(), ids=FORBIDDEN.keys())
def test_wrapped_forbidden_requests_are_rejected(request_):
    for wrapped in (
        functions.InvokeWithLayerRequest(LAYER, init_connection(request_)),
        functions.InvokeWithoutUpdatesRequest(request_),
        functions.InvokeWithLayerRequest(LAYER, functions.InvokeWithoutUpdatesRequest(init_connection(request_))),
        [functions.updates.GetStateRequest(), request_],
        [[functions.InvokeWithoutUpdatesRequest(request_)]],
    ):
        with pytest.raises(ReadOnlyViolation):
            check_request(wrapped)


def test_wrapped_read_requests_are_allowed():
    check_request(functions.InvokeWithLayerRequest(LAYER, functions.InvokeWithoutUpdatesRequest(
        init_connection(functions.help.GetConfigRequest()))))
    check_request([functions.InvokeWithoutUpdatesRequest(r) for r in ALLOWED.values()])


def test_unknown_wrappers_are_rejected_even_around_read_requests():
    history = ALLOWED["messages.GetHistory"]
    with pytest.raises(ReadOnlyViolation):
        check_request(functions.InvokeWithTakeoutRequest(1, history))
    with pytest.raises(ReadOnlyViolation):
        check_request(functions.InvokeAfterMsgRequest(1, history))


def test_login_requests_are_allowed_only_for_login():
    sign_in = functions.auth.SignInRequest("+1", "hash", "12345")
    with pytest.raises(ReadOnlyViolation):
        check_request(sign_in)
    check_request(sign_in, READ_ONLY_REQUESTS | LOGIN_REQUESTS)
    with pytest.raises(ReadOnlyViolation):
        check_request(FORBIDDEN["messages.ReadHistory"], READ_ONLY_REQUESTS | LOGIN_REQUESTS)


def _all_request_classes():
    import telethon.tl.functions as root

    modules = [root] + [getattr(root, name) for name in dir(root) if inspect.ismodule(getattr(root, name))]
    for module in modules:
        for value in vars(module).values():
            if inspect.isclass(value) and issubclass(value, TLRequest) and value is not TLRequest:
                yield value


MUTATING = re.compile(
    r"^(Read|Send|Edit|Delete|Join|Leave|Update|Save|Upload|Set|Toggle|Mark|View|Increment|Import|Report|Block|"
    r"Unblock|Install|Uninstall|Create|Add|Invite|Pin(?!g)|Unpin|Forward|Clear|Accept|Decline|Hide|Reorder|Migrate|"
    r"Reset|Confirm|Change|Register|Unregister|Discard|Start|Transcribe|Translate|Vote|Activate|Approve|"
    r"Reupload|Delete|Remove|Export|Finish|Init|Log|Sign|Check|Request|Prolong|Launch|Invoke)"
)


# Allowed although their names look like writes.
NOT_WRITES = {
    # Copy this session's own authorization to another data center to download media.
    functions.auth.ExportAuthorizationRequest,
    functions.auth.ImportAuthorizationRequest,
} | WRAPPERS


def test_every_mutating_request_in_the_schema_is_rejected():
    """Scan the whole TL schema: everything that looks like a write is refused."""
    classes = list(_all_request_classes())
    assert len(classes) > 500
    mutating = [cls for cls in classes if MUTATING.match(cls.__name__) and cls not in NOT_WRITES]
    assert len(mutating) > 300
    accepted = []
    for cls in mutating:
        try:
            check_request(cls.__new__(cls))
        except ReadOnlyViolation:
            continue
        accepted.append(f"{cls.__module__}.{cls.__name__}")
    assert accepted == []


# MCP_SAVED_MESSAGES_WRITE=1 adds exactly these two conditional entries.
WITH_SAVED = READ_ONLY_REQUESTS | SAVED_MESSAGES_REQUESTS


def _filled(cls, peer):
    """An instance of `cls` with every peer-like field set to `peer`, a plain text and one message id."""
    names = [name for name in inspect.signature(cls.__init__).parameters if name != "self"]
    request = cls.__new__(cls)
    for name in names:
        setattr(request, name, peer if "peer" in name else None)
    values = {"message": "note", "id": [1], "random_id": [7] if "id" in names else 7}
    for name, value in values.items():
        if name in names:
            setattr(request, name, value)
    return request


def _accepted(requests, allowed):
    accepted = set()
    for request in requests:
        try:
            check_request(request, allowed)
        except ReadOnlyViolation:
            continue
        accepted.add(type(request))
    return accepted


def test_schema_scan_with_saved_messages_write_accepts_exactly_two_requests_into_self():
    """The flag adds exactly two conditional writes, accepted only with the InputPeerSelf target."""
    mutating = [cls for cls in _all_request_classes() if MUTATING.match(cls.__name__) and cls not in NOT_WRITES]
    assert SAVED_MESSAGES_REQUESTS == {functions.messages.SendMessageRequest, functions.messages.ForwardMessagesRequest}
    assert SAVED_MESSAGES_REQUESTS <= set(mutating)
    # Half-built requests (no target at all) are rejected even with the flag.
    assert _accepted([cls.__new__(cls) for cls in mutating], WITH_SAVED) == set()
    to_self = [_filled(cls, types.InputPeerSelf()) for cls in mutating]
    assert _accepted(to_self, READ_ONLY_REQUESTS) == set()
    assert _accepted(to_self, WITH_SAVED) == SAVED_MESSAGES_REQUESTS
    # Any other peer, the owner's own InputPeerUser included, is rejected.
    for peer in (types.InputPeerUser(ME.id, ME.access_hash), PEER, types.InputPeerChat(10), CHANNEL_PEER,
                 types.InputPeerEmpty(), types.InputPeerUserFromMessage(CHANNEL_PEER, 1, ME.id)):
        assert _accepted([_filled(cls, peer) for cls in mutating], WITH_SAVED) == set(), peer


def test_allow_list_contains_only_reads():
    for cls in READ_ONLY_REQUESTS - NOT_WRITES:
        assert re.match(r"^(Get|Search|Resolve|Ping)", cls.__name__), cls
    assert functions.messages.GetMessagesViewsRequest not in READ_ONLY_REQUESTS  # increments view counters
    assert not READ_ONLY_REQUESTS & SAVED_MESSAGES_REQUESTS


def test_notify_settings_are_read_but_never_changed():
    assert functions.account.GetNotifySettingsRequest in READ_ONLY_REQUESTS
    for name, cls in vars(functions.account).items():
        if "Notify" in name and cls is not functions.account.GetNotifySettingsRequest:
            assert cls not in READ_ONLY_REQUESTS, name


def test_violation_is_not_mistaken_for_a_network_error():
    # Telethon's update loop and our startup code treat OSError as "network down" and retry.
    assert not issubclass(ReadOnlyViolation, OSError)


# Every place in Telethon that creates an MTProto sender, a client or sends on a sender, as
# reviewed for Telethon 1.45 (see guard.py). A new Telethon version that adds or changes one of
# them makes this test fail, so the guard is reviewed again before the upgrade.
REVIEWED_SEND_SITES = {
    ("client/chats.py", "return await sender.send(req)"),
    ("client/telegrambaseclient.py", "self._sender = MTProtoSender("),
    ("client/telegrambaseclient.py", "await self._sender.send(functions.InvokeWithLayerRequest(LAYER, req))"),
    ("client/telegrambaseclient.py", "sender = MTProtoSender(None, loggers=self._log)"),
    ("client/telegrambaseclient.py", "await sender.send(req)"),
    ("client/telegrambaseclient.py", "client = self.__class__("),
    ("client/users.py", "future = sender.send(request, ordered=ordered)"),
    ("network/mtprotoplainsender.py", "await self._connection.send("),
    ("network/mtprotosender.py", "future = sender.send(request)"),  # in a docstring
    ("network/mtprotosender.py", "plain = MTProtoPlainSender(self._connection, loggers=self._loggers)"),
    ("network/mtprotosender.py", "self.send(PingRequest(rnd_id))"),
    ("network/mtprotosender.py", "await self._connection.send(data)"),
    ("network/authenticator.py", "res_pq = await sender.send(ReqPqMultiRequest(nonce))"),
    ("network/authenticator.py", "server_dh_params = await sender.send(ReqDHParamsRequest("),
    ("network/authenticator.py", "dh_gen = await sender.send(SetClientDHParamsRequest("),
    ("sessions/abstract.py", "return to_instance or self.__class__()"),  # session clone, no network
}
SEND_SITE = re.compile(r"MTProto(Plain)?Sender\(|\.send\(|self\.__class__\(")


def test_telethon_send_paths_match_the_reviewed_version():
    root = Path(telethon.__file__).parent
    found = set()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith("tl/"):
            continue  # generated TL types, no network code
        for line in path.read_text(encoding="utf-8").splitlines():
            if SEND_SITE.search(line):
                found.add((relative, line.strip()))
    assert telethon.__version__.startswith("1.45."), "review guard.py against the new Telethon, then update this test"
    assert found == REVIEWED_SEND_SITES


# The real Telethon client with a recording transport in place of the network.


class Recorder:
    """Replaces `MTProtoSender.send`: records what would go on the wire and answers with canned results."""

    def __init__(self, answer):
        self.sent = []
        self.answer = answer

    def __call__(self, sender, request, ordered=False):
        self.sent.append(request)
        future = asyncio.get_running_loop().create_future()
        inner = request
        while type(inner) in (functions.InvokeWithoutUpdatesRequest, functions.InvokeWithLayerRequest,
                              functions.InitConnectionRequest):
            inner = inner.query
        future.set_result(self.answer(inner))
        return future

    def kinds(self):
        result = []
        for request in self.sent:
            while hasattr(request, "query"):
                request = request.query
            result.append(type(request).__name__)
        return result


NEWS = channel(50, "News", username="news", megagroup=True, forum=True, members=10)
ME = user(1000, "Owner", is_self=True)


def empty_answer(request):
    def slice_(**extra):
        return types.messages.ChannelMessages(pts=1, count=0, messages=[], topics=[], chats=[NEWS], users=[], **extra)

    answers = {
        functions.messages.GetDialogsRequest: lambda: types.messages.Dialogs([], [], [], []),
        functions.users.GetUsersRequest: lambda: [ME],
        functions.contacts.ResolveUsernameRequest: lambda: types.contacts.ResolvedPeer(
            peer=types.PeerChannel(50), chats=[NEWS], users=[]),
        functions.messages.GetHistoryRequest: slice_,
        functions.messages.GetRepliesRequest: slice_,
        functions.messages.SearchRequest: slice_,
        functions.channels.GetMessagesRequest: slice_,
        functions.messages.SearchGlobalRequest: lambda: types.messages.MessagesSlice(
            count=0, messages=[], topics=[], chats=[], users=[]),
        functions.messages.GetDialogFiltersRequest: lambda: types.messages.DialogFilters(
            filters=[types.DialogFilterDefault()]),
        functions.account.GetNotifySettingsRequest: lambda: types.PeerNotifySettings(),
        functions.channels.GetFullChannelRequest: lambda: types.messages.ChatFull(
            full_chat=types.ChannelFull(id=50, about="about", read_inbox_max_id=0, read_outbox_max_id=0,
                                        unread_count=0, chat_photo=types.PhotoEmpty(0),
                                        notify_settings=types.PeerNotifySettings(), bot_info=[], pts=1),
            chats=[NEWS], users=[]),
        functions.messages.GetForumTopicsRequest: lambda: types.messages.ForumTopics(
            count=0, topics=[], messages=[], chats=[NEWS], users=[], pts=1),
        functions.help.GetConfigRequest: lambda: types.Config,  # never inspected by the tests
    }
    return answers[type(request)]()


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder(empty_answer)
    monkeypatch.setattr(MTProtoSender, "send", lambda self, request, ordered=False: rec(self, request, ordered))
    return rec


@pytest.fixture
def client(recorder):
    return ReadOnlyTelegramClient(None, 1, "hash", receive_updates=False)


async def test_client_uses_guarded_sender(client):
    assert isinstance(client._sender, GuardedMTProtoSender)
    assert client._no_updates  # receive_updates=False


@pytest.mark.parametrize("request_", FORBIDDEN.values(), ids=FORBIDDEN.keys())
async def test_forbidden_requests_never_reach_the_transport(client, recorder, request_):
    with pytest.raises(ReadOnlyViolation):
        await client(request_)
    with pytest.raises(ReadOnlyViolation):
        client._sender.send(functions.InvokeWithLayerRequest(LAYER, init_connection(request_)))
    assert recorder.sent == []


async def test_high_level_write_methods_are_blocked(client, recorder):
    peer = types.InputPeerUser(1, 7)
    with pytest.raises(ReadOnlyViolation):
        await client.send_read_acknowledge(peer)
    with pytest.raises(ReadOnlyViolation):
        await client.send_message(peer, "hello")
    with pytest.raises(ReadOnlyViolation):
        await client.edit_message(peer, 1, "edited")
    with pytest.raises(ReadOnlyViolation):
        await client.delete_messages(peer, [1])
    with pytest.raises(ReadOnlyViolation):
        await client.delete_dialog(CHANNEL_PEER)
    assert recorder.sent == []


async def test_unguarded_sender_is_refused(client, recorder):
    with pytest.raises(ReadOnlyViolation):
        await client._call(MTProtoSender(None, loggers=client._log), functions.updates.GetStateRequest())
    assert recorder.sent == []


async def test_connection_setup_goes_through_the_guard(client, recorder):
    client._init_request.query = functions.help.GetConfigRequest()
    client._sender.send(functions.InvokeWithLayerRequest(LAYER, functions.InvokeWithoutUpdatesRequest(
        client._init_request)))
    assert recorder.kinds() == ["GetConfigRequest"]


async def test_exported_media_senders_are_guarded(client, recorder, monkeypatch):
    class Dc:
        id, ip_address, port = 4, "127.0.0.1", 443

    async def get_dc(dc_id, cdn=False):
        return Dc()

    async def connect(self, connection):
        return True

    recorder.answer = lambda request: (
        types.auth.ExportedAuthorization(id=1, bytes=b"x") if isinstance(request, functions.auth.ExportAuthorizationRequest)
        else types.auth.Authorization(user=ME)
    )
    monkeypatch.setattr(client, "_get_dc", get_dc)
    monkeypatch.setattr(MTProtoSender, "connect", connect)
    sender = await client._create_exported_sender(4)
    assert isinstance(sender, GuardedMTProtoSender)
    assert recorder.kinds() == ["ExportAuthorizationRequest", "ImportAuthorizationRequest"]
    with pytest.raises(ReadOnlyViolation):
        sender.send(FORBIDDEN["messages.ReadHistory"])
    with pytest.raises(ReadOnlyViolation):
        await client._call(sender, FORBIDDEN["messages.GetMessagesViews"])
    assert len(recorder.sent) == 2


async def test_every_tool_runs_through_the_real_client_within_the_allow_list(client, recorder, tmp_path):
    reader = TelegramReader(client)
    exporter = Exporter(reader, tmp_path, tmp_path)
    await reader.list_dialogs()
    await reader.list_folders()
    await reader.list_folder_chats(0)
    await reader.chat_info("@news")
    await reader.list_forum_topics("@news")
    await reader.read_messages("@news", limit=5)
    await reader.read_messages("@news", topic_id=1, oldest_first=True, since="2026-01-01")
    await reader.get_messages("@news", [1, 2])
    await reader.search_messages("@news", query="x", since="2026-01-01", until="2026-02-01", media="voice")
    await reader.search_global("x", chat_type="channel")
    await exporter.export_chat("@news", since="2026-01-01")
    kinds = set(recorder.kinds())
    assert kinds == {
        "GetDialogsRequest", "GetUsersRequest", "GetDialogFiltersRequest", "ResolveUsernameRequest",
        "GetFullChannelRequest", "GetForumTopicsRequest", "GetHistoryRequest", "GetRepliesRequest",
        "GetMessagesRequest", "SearchRequest", "SearchGlobalRequest", "GetNotifySettingsRequest",
    }
    for request in recorder.sent:
        check_request(request)  # nothing slipped through
        assert isinstance(request, functions.InvokeWithoutUpdatesRequest)  # receive_updates=False


# Opt-in writes into the own Saved Messages (MCP_SAVED_MESSAGES_WRITE=1).

SELF = types.InputPeerSelf()


def self_send(**extra):
    return functions.messages.SendMessageRequest(SELF, "note", no_webpage=True, **extra)


def self_forward(**extra):
    return functions.messages.ForwardMessagesRequest(CHANNEL_PEER, [1, 2], SELF, **extra)


def test_saved_messages_writes_need_the_flag():
    for request in (self_send(), self_forward()):
        with pytest.raises(ReadOnlyViolation):
            check_request(request)
        with pytest.raises(ReadOnlyViolation):
            check_request(request, READ_ONLY_REQUESTS | LOGIN_REQUESTS)
        check_request(request, WITH_SAVED)
    # From any chat, including Saved Messages itself.
    check_request(functions.messages.ForwardMessagesRequest(SELF, [1], SELF), WITH_SAVED)
    check_request(functions.messages.ForwardMessagesRequest(PEER, [1], SELF), WITH_SAVED)
    check_request(functions.messages.SendMessageRequest(SELF, "note"), WITH_SAVED)


@pytest.mark.parametrize("request_", FORBIDDEN.values(), ids=FORBIDDEN.keys())
def test_forbidden_requests_stay_rejected_with_the_flag(request_):
    for wrapped in (request_, functions.InvokeWithoutUpdatesRequest(request_),
                    functions.InvokeWithLayerRequest(LAYER, init_connection(request_)), [self_send(), request_]):
        with pytest.raises(ReadOnlyViolation):
            check_request(wrapped, WITH_SAVED)


OTHER_PEERS = {
    "own id as InputPeerUser": types.InputPeerUser(ME.id, ME.access_hash),
    "user": PEER,
    "basic group": types.InputPeerChat(10),
    "channel": CHANNEL_PEER,
    "empty": types.InputPeerEmpty(),
    "user from message": types.InputPeerUserFromMessage(CHANNEL_PEER, 1, ME.id),
    "channel from message": types.InputPeerChannelFromMessage(CHANNEL_PEER, 1, 51),
}


@pytest.mark.parametrize("peer", OTHER_PEERS.values(), ids=OTHER_PEERS.keys())
def test_saved_messages_writes_to_any_other_peer_are_rejected(peer):
    for request in (functions.messages.SendMessageRequest(peer, "note"),
                    functions.messages.ForwardMessagesRequest(CHANNEL_PEER, [1], peer)):
        with pytest.raises(ReadOnlyViolation, match="InputPeerSelf"):
            check_request(request, WITH_SAVED)


OPTIONS = {
    "send_as": dict(send_as=CHANNEL_PEER),
    "send_as self": dict(send_as=SELF),
    "schedule_date": dict(schedule_date=datetime(2030, 1, 1, tzinfo=timezone.utc)),
    "schedule_repeat_period": dict(schedule_repeat_period=86400),
    "quick_reply_shortcut": dict(quick_reply_shortcut=types.InputQuickReplyShortcut("x")),
    "effect": dict(effect=1),
    "allow_paid_stars": dict(allow_paid_stars=1),
    "allow_paid_floodskip": dict(allow_paid_floodskip=True),
    "noforwards": dict(noforwards=True),
    "silent": dict(silent=True),
    "background": dict(background=True),
    "reply_to another peer": dict(reply_to=types.InputReplyToMessage(1, reply_to_peer_id=PEER)),
    "suggested_post": dict(suggested_post=types.SuggestedPost()),
}
SEND_ONLY = {
    "reply_to": dict(reply_to=types.InputReplyToMessage(1)),
    "entities": dict(entities=[types.MessageEntityBold(0, 4)]),
    "reply_markup": dict(reply_markup=types.ReplyKeyboardHide()),
    "clear_draft": dict(clear_draft=True),
    "invert_media": dict(invert_media=True),
    "update_stickersets_order": dict(update_stickersets_order=True),
}
FORWARD_ONLY = {
    "top_msg_id": dict(top_msg_id=1),
    "drop_author": dict(drop_author=True),
    "drop_media_captions": dict(drop_media_captions=True),
    "with_my_score": dict(with_my_score=True),
    "video_timestamp": dict(video_timestamp=5),
    "from_ephemeral": dict(from_ephemeral=True),
}


@pytest.mark.parametrize("make,extra", [(self_send, v) for v in {**OPTIONS, **SEND_ONLY}.values()]
                         + [(self_forward, v) for v in {**OPTIONS, **FORWARD_ONLY}.values()],
                         ids=[f"send {k}" for k in {**OPTIONS, **SEND_ONLY}]
                         + [f"forward {k}" for k in {**OPTIONS, **FORWARD_ONLY}])
def test_saved_messages_writes_with_options_are_rejected(make, extra):
    with pytest.raises(ReadOnlyViolation, match="only plain delivery"):
        check_request(make(**extra), WITH_SAVED)


@pytest.mark.parametrize("make", [self_send, self_forward])
def test_every_other_field_must_be_unset(make):
    """Generic: any field outside the permitted few, set to anything, is rejected (future fields too)."""
    permitted = {"peer", "message", "random_id", "no_webpage", "from_peer", "id", "to_peer"}
    fields = [f for f in inspect.signature(type(make()).__init__).parameters if f not in permitted | {"self"}]
    assert len(fields) >= 15
    for field in fields:
        request = make()
        setattr(request, field, object())
        with pytest.raises(ReadOnlyViolation, match=field):
            check_request(request, WITH_SAVED)


def test_malformed_saved_messages_writes_are_rejected():
    bad = [
        functions.messages.SendMessageRequest(SELF, ""),
        functions.messages.SendMessageRequest(SELF, None),
        functions.messages.ForwardMessagesRequest(types.InputPeerEmpty(), [1], SELF),
        functions.messages.ForwardMessagesRequest(CHANNEL, [1], SELF),  # not an InputPeer
        functions.messages.ForwardMessagesRequest(CHANNEL_PEER, [], SELF),
        functions.messages.ForwardMessagesRequest(CHANNEL_PEER, [1, 2], SELF, random_id=[1]),
        functions.messages.ForwardMessagesRequest(CHANNEL_PEER, ["1"], SELF, random_id=[1]),
    ]
    for request in bad:
        with pytest.raises(ReadOnlyViolation):
            check_request(request, WITH_SAVED)


def test_saved_messages_writes_in_wrappers_and_batches():
    for request in (self_send(), self_forward()):
        check_request(functions.InvokeWithoutUpdatesRequest(request), WITH_SAVED)
        check_request(functions.InvokeWithLayerRequest(LAYER, init_connection(request)), WITH_SAVED)
        check_request([request, functions.updates.GetStateRequest()], WITH_SAVED)
        for wrapper in (functions.InvokeWithTakeoutRequest(1, request), functions.InvokeAfterMsgRequest(1, request)):
            with pytest.raises(ReadOnlyViolation):
                check_request(wrapper, WITH_SAVED)
        with pytest.raises(ReadOnlyViolation):
            check_request(functions.InvokeWithoutUpdatesRequest(request))  # flag off
    # One bad item rejects the whole batch.
    for batch in ([self_send(), functions.messages.SendMessageRequest(PEER, "x")],
                  [self_forward(), self_send(send_as=CHANNEL_PEER)],
                  [[functions.InvokeWithoutUpdatesRequest(self_forward())], FORBIDDEN["messages.ReadHistory"]]):
        with pytest.raises(ReadOnlyViolation):
            check_request(batch, WITH_SAVED)


def saved_answer(request):
    if isinstance(request, functions.messages.SendMessageRequest):
        return types.UpdateShortSentMessage(out=True, id=501, pts=1, pts_count=1,
                                            date=datetime(2026, 1, 1, tzinfo=timezone.utc))
    if isinstance(request, functions.messages.ForwardMessagesRequest):
        date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        updates = []
        for i, random_id in enumerate(request.random_id):
            copy = types.Message(id=600 + i, peer_id=types.PeerUser(ME.id), date=date, message="post", out=True,
                                 fwd_from=types.MessageFwdHeader(date=date, from_id=types.PeerChannel(50)))
            updates += [types.UpdateMessageID(id=600 + i, random_id=random_id),
                        types.UpdateNewMessage(copy, pts=1, pts_count=1)]
        return types.Updates(updates=updates, users=[ME], chats=[NEWS], date=date, seq=0)
    if isinstance(request, functions.channels.GetMessagesRequest):
        messages = [types.Message(id=i.id, peer_id=types.PeerChannel(50), date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                  message=f"post {i.id}") for i in request.id]
        return types.messages.ChannelMessages(pts=1, count=len(messages), messages=messages, topics=[],
                                              chats=[NEWS], users=[])
    return empty_answer(request)


@pytest.fixture
def saved_client(recorder):
    recorder.answer = saved_answer
    return ReadOnlyTelegramClient(None, 1, "hash", receive_updates=False, allowed=WITH_SAVED)


async def test_telethon_high_level_saved_messages_methods_issue_only_the_allowed_requests(saved_client, recorder):
    """What Telethon really sends for send_message("me") / forward_messages("me") (reviewed for 1.45)."""
    sent = await saved_client.send_message("me", "plain **text**", parse_mode=None, link_preview=False)
    assert sent.id == 501
    forwarded = await saved_client.forward_messages("me", [3, 4], from_peer=CHANNEL_PEER)
    assert [m.id for m in forwarded] == [600, 601]
    kinds = recorder.kinds()
    # `_get_peer` of the self target may ask for the own user (a read); nothing else is sent.
    assert [k for k in kinds if k != "GetUsersRequest"] == ["SendMessageRequest", "ForwardMessagesRequest"]
    for request in recorder.sent:
        check_request(request, WITH_SAVED)
        assert type(request.query) is not functions.InvokeWithLayerRequest
    send = recorder.sent[kinds.index("SendMessageRequest")].query
    assert isinstance(send.peer, types.InputPeerSelf) and send.entities in (None, [])


async def test_telethon_high_level_writes_elsewhere_or_with_options_are_blocked(saved_client, recorder):
    attempts = [
        saved_client.send_message(PEER, "x"),
        saved_client.send_message("me", "**markdown** creates entities"),
        saved_client.send_message("me", "x", schedule=datetime(2030, 1, 1, tzinfo=timezone.utc)),
        saved_client.send_message("me", "x", send_as=CHANNEL_PEER),
        saved_client.send_message("me", "x", silent=True),
        saved_client.send_message("me", "x", reply_to=5),
        saved_client.forward_messages(PEER, [1], from_peer=CHANNEL_PEER),
        saved_client.forward_messages("me", [1], from_peer=CHANNEL_PEER, schedule=datetime(2030, 1, 1, tzinfo=timezone.utc)),
        saved_client.forward_messages("me", [1], from_peer=CHANNEL_PEER, drop_author=True),
        saved_client.edit_message("me", 1, "edited"),
        saved_client.delete_messages("me", [1]),
        saved_client.send_read_acknowledge("me"),
    ]
    for attempt in attempts:
        with pytest.raises(ReadOnlyViolation):
            await attempt
    assert [k for k in recorder.kinds() if k != "GetUsersRequest"] == []


async def test_saved_messages_methods_are_blocked_without_the_flag(client, recorder):
    with pytest.raises(ReadOnlyViolation):
        await client.send_message("me", "x", parse_mode=None)
    with pytest.raises(ReadOnlyViolation):
        await client.forward_messages("me", [1], from_peer=CHANNEL_PEER)
    assert [k for k in recorder.kinds() if k != "GetUsersRequest"] == []


async def test_saved_messages_writer_runs_through_the_real_client_within_the_guard(saved_client, recorder):
    writer = SavedMessagesWriter(TelegramReader(saved_client))
    assert await writer.send_to_saved("a" * 5000) == {"message_ids": [501, 501]}
    result = await writer.forward_to_saved("@news", [3, 4])
    assert result == {"message_ids": [600, 601]}
    kinds = recorder.kinds()
    assert kinds.count("SendMessageRequest") == 2 and kinds.count("ForwardMessagesRequest") == 1
    assert set(kinds) <= {"SendMessageRequest", "ForwardMessagesRequest", "GetDialogsRequest",
                          "ResolveUsernameRequest", "GetMessagesRequest", "GetNotifySettingsRequest"}
    for request in recorder.sent:
        check_request(request, WITH_SAVED)
        assert isinstance(request, functions.InvokeWithoutUpdatesRequest)
    forward = [r.query for r in recorder.sent if isinstance(r.query, functions.messages.ForwardMessagesRequest)][0]
    assert isinstance(forward.to_peer, types.InputPeerSelf)
    assert isinstance(forward.from_peer, types.InputPeerChannel) and forward.id == [3, 4]
