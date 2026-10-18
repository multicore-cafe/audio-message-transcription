"""An in-memory Telegram account for the MCP server tests, built from real Telethon TL objects.

`FakeTelegramClient` implements only the read methods the reader uses. Every raw request goes
through the read-only guard first, so the tests also prove the tools stay within the allow-list.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import errors, types, utils
from telethon._updates import EntityCache as MbEntityCache
from telethon.tl.custom import Dialog

from transcription_bot.mcp_server.guard import check_request

ME_ID = 1000
BASE_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def user(id, first, last=None, *, username=None, contact=False, bot=False, is_self=False, phone=None):
    return types.User(id=id, first_name=first, last_name=last, username=username, contact=contact, bot=bot,
                      is_self=is_self, access_hash=id * 7, phone=phone)


def basic_group(id, title, members=3):
    return types.Chat(id=id, title=title, photo=types.ChatPhotoEmpty(), participants_count=members,
                      date=BASE_DATE, version=1)


def channel(id, title, *, username=None, megagroup=False, forum=False, members=None):
    return types.Channel(id=id, title=title, photo=types.ChatPhotoEmpty(), date=BASE_DATE, access_hash=id * 3,
                         username=username, megagroup=megagroup, broadcast=not megagroup, forum=forum,
                         participants_count=members)


def document(name="file.bin", size=10, mime="application/octet-stream", attributes=()):
    return types.MessageMediaDocument(document=types.Document(
        id=1, access_hash=1, file_reference=b"", date=BASE_DATE, mime_type=mime, size=size, dc_id=2,
        attributes=[types.DocumentAttributeFilename(name), *attributes],
    ))


def voice(seconds=3):
    media = document("voice.ogg", 100, "audio/ogg", [types.DocumentAttributeAudio(duration=seconds, voice=True)])
    media.voice = True
    return media


class FakeTelegramClient:
    def __init__(self):
        self._self_id = ME_ID
        self._mb_entity_cache = MbEntityCache()
        self.entities: dict[int, object] = {}
        self.dialog_rows: list[tuple[object, dict]] = []
        self.messages: dict[int, list[types.Message]] = {}
        self.filters: list = []
        self.topics: dict[int, list[types.ForumTopic]] = {}
        self.full: dict[int, object] = {}
        self.requests: list = []
        self.dialog_loads = 0
        # Default notification settings per scope ("InputNotifyUsers", "InputNotifyChats", "InputNotifyBroadcasts").
        self.notify_defaults: dict[str, types.PeerNotifySettings] = {}
        self.me = self.add(user(ME_ID, "Owner", username="owner", is_self=True))

    # Building the account

    def add(self, entity):
        self.entities[utils.get_peer_id(entity)] = entity
        return entity

    def add_dialog(self, entity, *, unread=0, mentions=0, reactions=0, marked=False, muted=False,
                   archived=False, pinned=False):
        """`muted`: True (muted), False (no own setting, the default of the chat type applies) or "unmuted"."""
        self.add(entity)
        self.dialog_rows.append((entity, dict(unread=unread, mentions=mentions, reactions=reactions, marked=marked,
                                              muted=muted, archived=archived, pinned=pinned)))
        return entity

    def add_message(self, chat, id, text="", *, minutes=None, sender=None, media=None, reply_to=None,
                    topic=None, fwd_from=None, reactions=None, action=None):
        peer = utils.get_peer(chat)
        date = BASE_DATE + timedelta(minutes=id if minutes is None else minutes)
        if topic is not None:
            reply_to = types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=reply_to or topic,
                                                reply_to_top_id=topic if reply_to else None)
        elif reply_to is not None:
            reply_to = types.MessageReplyHeader(reply_to_msg_id=reply_to)
        from_id = utils.get_peer(sender) if sender is not None else None
        common = dict(id=id, peer_id=peer, date=date, from_id=from_id, reply_to=reply_to,
                      out=sender is not None and utils.get_peer_id(sender) == ME_ID)
        if action is not None:
            message = types.MessageService(action=action, **common)
        else:
            message = types.Message(message=text, media=media, fwd_from=fwd_from, reactions=reactions, **common)
        message._finish_init(self, self.entities, None)
        self.messages.setdefault(utils.get_peer_id(chat), []).append(message)
        self.messages[utils.get_peer_id(chat)].sort(key=lambda m: m.id)
        return message

    # Telethon-like read API

    async def get_me(self):
        return self.me

    async def iter_dialogs(self, **kwargs):
        self.dialog_loads += 1
        rows = sorted(self.dialog_rows, key=lambda row: -self._last_id(row[0]))
        rows.sort(key=lambda row: not row[1]["pinned"])
        for entity, opts in rows:
            peer_id = utils.get_peer_id(entity)
            last = self.messages.get(peer_id, [None])[-1]
            raw = types.Dialog(
                peer=utils.get_peer(entity), top_message=last.id if last else 0, read_inbox_max_id=0,
                read_outbox_max_id=0, unread_count=opts["unread"], unread_mentions_count=opts["mentions"],
                unread_reactions_count=opts["reactions"], unread_poll_votes_count=0,
                notify_settings=types.PeerNotifySettings(mute_until={
                    True: datetime(2038, 1, 1, tzinfo=timezone.utc), False: None,
                    "unmuted": datetime(1970, 1, 1, tzinfo=timezone.utc)}[opts["muted"]]),
                pinned=opts["pinned"], unread_mark=opts["marked"], folder_id=1 if opts["archived"] else None,
            )
            yield Dialog(self, raw, self.entities, last)

    def _last_id(self, entity) -> int:
        messages = self.messages.get(utils.get_peer_id(entity))
        return messages[-1].id if messages else 0

    async def get_entity(self, ref):
        if isinstance(ref, int) and ref in self.entities:
            return self.entities[ref]
        raise ValueError(f"Could not find the input entity for {ref!r}")

    async def get_input_entity(self, ref):
        return utils.get_input_peer(await self.get_entity(ref))

    async def iter_messages(self, entity, limit=None, *, offset_id=0, min_id=0, max_id=0, offset_date=None,
                            reverse=False, reply_to=None):
        messages = list(self.messages.get(utils.get_peer_id(entity), []))
        if reply_to:
            messages = [m for m in messages if m.reply_to and reply_to in (
                m.reply_to.reply_to_msg_id, m.reply_to.reply_to_top_id)]
        if min_id:
            messages = [m for m in messages if m.id > min_id]
        if max_id:
            messages = [m for m in messages if m.id < max_id]
        if reverse:
            if offset_id:
                messages = [m for m in messages if m.id > offset_id]
            elif offset_date:
                messages = [m for m in messages if m.date >= offset_date]
        else:
            if offset_id:
                messages = [m for m in messages if m.id < offset_id]
            if offset_date:
                messages = [m for m in messages if m.date < offset_date]
            messages.reverse()
        for message in messages[:limit]:
            yield message

    async def get_messages(self, entity, ids):
        by_id = {m.id: m for m in self.messages.get(utils.get_peer_id(entity), [])}
        return [by_id.get(i) for i in ids]

    async def download_media(self, message, file):
        Path(file).write_bytes(b"media-of-%d" % message.id)
        return file

    # Raw requests

    async def __call__(self, request):
        check_request(request)
        self.requests.append(request)
        handler = getattr(self, "_" + type(request).__name__, None)
        if handler is None:
            raise AssertionError(f"unexpected request {type(request).__name__}")
        return handler(request)

    def _everyone(self):
        users = [e for e in self.entities.values() if isinstance(e, types.User)]
        chats = [e for e in self.entities.values() if not isinstance(e, types.User)]
        return users, chats

    def _GetNotifySettingsRequest(self, request):
        return self.notify_defaults.get(type(request.peer).__name__, types.PeerNotifySettings())

    def _GetDialogFiltersRequest(self, request):
        return types.messages.DialogFilters(filters=list(self.filters))

    def _ResolveUsernameRequest(self, request):
        for entity in self.entities.values():
            if (getattr(entity, "username", None) or "").lower() == request.username.lower():
                users, chats = self._everyone()
                return types.contacts.ResolvedPeer(peer=utils.get_peer(entity), chats=chats, users=users)
        raise errors.UsernameNotOccupiedError(request)

    def _GetFullChannelRequest(self, request):
        users, chats = self._everyone()
        return types.messages.ChatFull(full_chat=self.full[utils.get_peer_id(request.channel)], chats=chats,
                                       users=users)

    def _GetFullChatRequest(self, request):
        users, chats = self._everyone()
        return types.messages.ChatFull(full_chat=self.full[-request.chat_id], chats=chats, users=users)

    def _GetFullUserRequest(self, request):
        users, chats = self._everyone()
        return types.users.UserFull(full_user=self.full[utils.get_peer_id(request.id)], chats=chats, users=users)

    def _GetForumTopicsRequest(self, request):
        topics = self.topics.get(utils.get_peer_id(request.peer), [])
        if request.offset_topic:
            topics = [t for t in topics if t.id < request.offset_topic]
        page = topics[:request.limit]
        users, chats = self._everyone()
        return types.messages.ForumTopics(count=len(self.topics.get(utils.get_peer_id(request.peer), [])),
                                          topics=page, messages=[], chats=chats, users=users, pts=1)

    def _matches(self, message, request) -> bool:
        if request.q and request.q.lower() not in (message.message or "").lower():
            return False
        if request.min_date and message.date < request.min_date:
            return False
        if request.max_date and message.date >= request.max_date:
            return False
        kind = type(request.filter).__name__
        if kind == "InputMessagesFilterVoice" and not message.voice:
            return False
        if kind == "InputMessagesFilterDocument" and not message.document:
            return False
        return kind in ("InputMessagesFilterEmpty", "InputMessagesFilterVoice", "InputMessagesFilterDocument")

    def _SearchRequest(self, request):
        found = [m for m in reversed(self.messages.get(utils.get_peer_id(request.peer), []))
                 if self._matches(m, request)]
        if request.from_id is not None and not isinstance(request.peer, types.InputPeerUser):
            found = [m for m in found if m.sender_id == utils.get_peer_id(request.from_id)]
        if request.top_msg_id:
            found = [m for m in found if m.reply_to and request.top_msg_id in (
                m.reply_to.reply_to_msg_id, m.reply_to.reply_to_top_id)]
        if request.offset_id:
            found = [m for m in found if m.id < request.offset_id]
        users, chats = self._everyone()
        return types.messages.MessagesSlice(count=len(found), messages=found[:request.limit], topics=[],
                                            chats=chats, users=users)

    def _SearchGlobalRequest(self, request):
        found = [m for chat in self.messages.values() for m in chat if self._matches(m, request)]
        found.sort(key=lambda m: (m.date, m.id), reverse=True)
        if request.offset_rate:
            found = [m for m in found if (m.date.timestamp(), m.id) < (request.offset_rate, request.offset_id)]
        page = found[:request.limit]
        users, chats = self._everyone()
        return types.messages.MessagesSlice(
            count=len(found), messages=page, topics=[], chats=chats, users=users,
            next_rate=int(page[-1].date.timestamp()) if page else None,
        )
