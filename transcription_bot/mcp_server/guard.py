"""Read-only enforcement: every outgoing TL request is checked against an explicit allow-list.

The check runs in two places of the Telethon client:

* `ReadOnlyTelegramClient._call`, the entry point of `client(request)` and of every high-level
  method, before Telethon resolves or wraps anything;
* `GuardedMTProtoSender.send`, the single method through which Telethon hands requests to the
  network for the main connection and for the per-DC connections used by media downloads. It
  also sees the requests Telethon sends directly on connect (`InvokeWithLayer(InitConnection(...)))`.

Anything that is not on the allow-list raises `ReadOnlyViolation` before it is queued for
sending. Wrapper requests (`InvokeWithLayer`, `InitConnection`, `InvokeWithoutUpdates`) are
unwrapped and their inner query is checked too; lists of requests are checked item by item and
rejected as a whole if one item is not allowed. Unknown wrappers (takeout, business connection
and so on) are not on the list and are therefore rejected.

The only writes that can ever be allowed are in `SAVED_MESSAGES_REQUESTS` (opt-in, see
`MCP_SAVED_MESSAGES_WRITE`): a plain text message or a plain forward into the owner's own Saved
Messages. They are conditional: even when allowed, a request passes only if it targets
`InputPeerSelf` and sets no option beyond plain delivery (no send_as, scheduling, replies,
entities, effects, paid options and so on).
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Collection

from telethon import TelegramClient, functions, types
from telethon.client.telegrambaseclient import LAYER
from telethon.network import MTProtoSender

log = logging.getLogger(__name__)


class ReadOnlyViolation(RuntimeError):
    """A request that could change the account (or mark something as read) was blocked.

    Not an `OSError`, so network error handlers (ours and Telethon's) never mistake it for an outage.
    """


# Requests whose only purpose is to carry another request; the inner `query` is checked.
WRAPPERS: frozenset[type] = frozenset({
    functions.InvokeWithLayerRequest,
    functions.InitConnectionRequest,
    functions.InvokeWithoutUpdatesRequest,
})

# Everything the server and Telethon's own machinery need. Nothing here changes account state,
# marks messages or reactions as read, or increments view counters.
READ_ONLY_REQUESTS: frozenset[type] = frozenset({
    # Connection setup and keep-alive.
    functions.PingRequest,
    functions.help.GetConfigRequest,
    # Update state, used on connect and by the connection watchdog (no updates are received).
    functions.updates.GetStateRequest,
    functions.updates.GetDifferenceRequest,
    functions.updates.GetChannelDifferenceRequest,
    # Media lives on other data centers: copy this session's authorization there.
    functions.auth.ExportAuthorizationRequest,
    functions.auth.ImportAuthorizationRequest,
    functions.upload.GetFileRequest,
    # Users, chats and channels.
    functions.users.GetUsersRequest,
    functions.users.GetFullUserRequest,
    functions.contacts.ResolveUsernameRequest,
    functions.messages.GetChatsRequest,
    functions.messages.GetFullChatRequest,
    functions.channels.GetChannelsRequest,
    functions.channels.GetFullChannelRequest,
    # Default notification settings per chat type (users, groups, channels), for "muted".
    functions.account.GetNotifySettingsRequest,
    # Dialogs, folders and forum topics.
    functions.messages.GetDialogsRequest,
    functions.messages.GetPeerDialogsRequest,
    functions.messages.GetDialogFiltersRequest,
    functions.messages.GetForumTopicsRequest,
    # Messages and search.
    functions.messages.GetHistoryRequest,
    functions.messages.GetMessagesRequest,
    functions.channels.GetMessagesRequest,
    functions.messages.GetRepliesRequest,
    functions.messages.SearchRequest,
    functions.messages.SearchGlobalRequest,
})

# Extra requests needed only by the interactive `login` command (sign-in creates the session).
LOGIN_REQUESTS: frozenset[type] = frozenset({
    functions.auth.SendCodeRequest,
    functions.auth.ResendCodeRequest,
    functions.auth.SignInRequest,
    functions.account.GetPasswordRequest,
    functions.auth.CheckPasswordRequest,
})


# Opt-in writes into the owner's own Saved Messages; always checked by `check_saved_messages_request`.
SAVED_MESSAGES_REQUESTS: frozenset[type] = frozenset({
    functions.messages.SendMessageRequest,
    functions.messages.ForwardMessagesRequest,
})

# Per conditional request: the field that must be `InputPeerSelf` and the only fields that may be
# set. Every other field (send_as, schedule_date, reply_to, entities, effect, quick_reply_shortcut,
# top_msg_id, allow_paid_*, noforwards, drop_author, ... and any field a newer layer adds) must be
# unset: None, False or an empty list.
_SAVED_MESSAGES_FIELDS: dict[type, tuple[str, frozenset[str]]] = {
    functions.messages.SendMessageRequest: ("peer", frozenset({"peer", "message", "random_id", "no_webpage"})),
    functions.messages.ForwardMessagesRequest: ("to_peer", frozenset({"from_peer", "id", "to_peer", "random_id"})),
}
_INPUT_PEER = types.InputPeerSelf.SUBCLASS_OF_ID


def _unset(value) -> bool:
    return value is None or value is False or (isinstance(value, (list, tuple)) and not value)


def _is_int_list(value) -> bool:
    return isinstance(value, list) and bool(value) and all(type(item) is int for item in value)


def check_saved_messages_request(request) -> None:
    """Raise `ReadOnlyViolation` unless `request` is a plain send/forward into the own Saved Messages."""
    kind = type(request)
    name = kind.__name__
    target, permitted = _SAVED_MESSAGES_FIELDS[kind]
    if type(getattr(request, target, None)) is not types.InputPeerSelf:
        raise ReadOnlyViolation(f"{name} is allowed only into the own Saved Messages (InputPeerSelf)")
    for field in inspect.signature(kind.__init__).parameters:
        if field != "self" and field not in permitted and not _unset(getattr(request, field, None)):
            raise ReadOnlyViolation(f"{name} with {field} is not allowed: only plain delivery to Saved Messages")
    if kind is functions.messages.SendMessageRequest:
        message = getattr(request, "message", None)
        ok = (isinstance(message, str) and bool(message) and type(getattr(request, "random_id", None)) is int
              and getattr(request, "no_webpage", None) in (None, True, False))
    else:
        from_peer = getattr(request, "from_peer", None)
        ok = (getattr(from_peer, "SUBCLASS_OF_ID", None) == _INPUT_PEER
              and not isinstance(from_peer, types.InputPeerEmpty)
              and _is_int_list(getattr(request, "id", None)) and _is_int_list(getattr(request, "random_id", None))
              and len(request.id) == len(request.random_id))
    if not ok:
        raise ReadOnlyViolation(f"{name} is malformed for a Saved Messages write")


def check_request(request, allowed: Collection[type] = READ_ONLY_REQUESTS) -> None:
    """Raise `ReadOnlyViolation` unless `request` (and everything nested in it) is allowed."""
    if isinstance(request, (list, tuple)):
        for item in request:
            check_request(item, allowed)
        return
    kind = type(request)
    if kind in WRAPPERS:
        check_request(request.query, allowed)
        return
    if kind not in allowed:
        raise ReadOnlyViolation(f"request {kind.__name__} is not allowed by the read-only guard")
    if kind in SAVED_MESSAGES_REQUESTS:
        check_saved_messages_request(request)


class GuardedMTProtoSender(MTProtoSender):
    """MTProto sender that refuses to queue requests outside its allow-list."""

    allowed: Collection[type] = READ_ONLY_REQUESTS

    def send(self, request, ordered=False):
        try:
            check_request(request, self.allowed)
        except ReadOnlyViolation as exc:
            log.error("blocked outgoing request: %s", exc)
            raise
        return super().send(request, ordered)


class ReadOnlyTelegramClient(TelegramClient):
    """Telethon client that can only send the requests in `allowed` (read-only by default)."""

    def __init__(self, *args, allowed: Collection[type] = READ_ONLY_REQUESTS, **kwargs):
        super().__init__(*args, **kwargs)
        self._allowed = frozenset(allowed)
        self._sender = self._guard_sender(self._sender)

    def _guard_sender(self, sender: MTProtoSender) -> GuardedMTProtoSender:
        # The subclass adds no state, so switching the class keeps everything Telethon set up.
        sender.__class__ = GuardedMTProtoSender
        sender.allowed = self._allowed
        return sender

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        check_request(request, self._allowed)
        if not isinstance(sender, GuardedMTProtoSender):
            raise ReadOnlyViolation("refusing to send through an unguarded connection")
        return await super()._call(sender, request, ordered, flood_sleep_threshold)

    async def _create_exported_sender(self, dc_id):
        # Same as Telethon's implementation, but the per-DC connection is guarded as well.
        dc = await self._get_dc(dc_id)
        sender = self._guard_sender(MTProtoSender(None, loggers=self._log))
        await sender.connect(self._connection(
            dc.ip_address, dc.port, dc.id, loggers=self._log, proxy=self._proxy, local_addr=self._local_addr,
        ))
        self._log[__name__].info("Exporting auth for new borrowed sender in %s", dc)
        auth = await self(functions.auth.ExportAuthorizationRequest(dc_id))
        self._init_request.query = functions.auth.ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
        await sender.send(functions.InvokeWithLayerRequest(LAYER, self._init_request))
        return sender

    async def send_read_acknowledge(self, *args, **kwargs):
        raise ReadOnlyViolation("send_read_acknowledge is disabled: this client never marks anything as read")
