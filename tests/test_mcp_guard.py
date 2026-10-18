"""The read-only guard: forbidden requests never reach the network, the ones the tools use do."""

import asyncio
import inspect
import re

import pytest
from telethon import functions, types
from telethon.client.telegrambaseclient import LAYER
from telethon.network import MTProtoSender
from telethon.tl.tlobject import TLRequest

from tests.mcp_fakes import channel, user
from transcription_bot.mcp_server.export import Exporter
from transcription_bot.mcp_server.guard import (
    LOGIN_REQUESTS,
    READ_ONLY_REQUESTS,
    WRAPPERS,
    GuardedMTProtoSender,
    ReadOnlyTelegramClient,
    ReadOnlyViolation,
    check_request,
)
from transcription_bot.mcp_server.reader import TelegramReader

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


def test_allow_list_contains_only_reads():
    for cls in READ_ONLY_REQUESTS - NOT_WRITES:
        assert re.match(r"^(Get|Search|Resolve|Ping)", cls.__name__), cls
    assert functions.messages.GetMessagesViewsRequest not in READ_ONLY_REQUESTS  # increments view counters


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
        "GetMessagesRequest", "SearchRequest", "SearchGlobalRequest",
    }
    for request in recorder.sent:
        check_request(request)  # nothing slipped through
        assert isinstance(request, functions.InvokeWithoutUpdatesRequest)  # receive_updates=False
