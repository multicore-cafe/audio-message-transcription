"""Read-only Telegram operations behind the MCP tools. Results are plain JSON-friendly dicts."""

from __future__ import annotations

import itertools
import json
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from telethon import functions, types, utils

from .folders import ARCHIVE_FOLDER_ID, DialogFacts, Folder, parse_filter
from .serialize import chat_kind, entity_ref, iso, message_dict, usernames

MAX_LIMIT = 100
MAX_DIALOG_LIMIT = 500
MAX_TEXT = 4096

MEDIA_FILTERS: dict[str, type] = {
    "photo": types.InputMessagesFilterPhotos,
    "video": types.InputMessagesFilterVideo,
    "photo_video": types.InputMessagesFilterPhotoVideo,
    "document": types.InputMessagesFilterDocument,
    "audio": types.InputMessagesFilterMusic,
    "voice": types.InputMessagesFilterVoice,
    "video_note": types.InputMessagesFilterRoundVideo,
    "voice_or_video_note": types.InputMessagesFilterRoundVoice,
    "gif": types.InputMessagesFilterGif,
    "url": types.InputMessagesFilterUrl,
    "poll": types.InputMessagesFilterPoll,
    "location": types.InputMessagesFilterGeo,
    "contact": types.InputMessagesFilterContacts,
    "pinned": types.InputMessagesFilterPinned,
    "mentions": types.InputMessagesFilterMyMentions,
}
CHAT_TYPES = ("user", "bot", "group", "channel")
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_LINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/", re.IGNORECASE)


class ReaderError(ValueError):
    """Bad arguments or a chat that cannot be found; the message is shown to the model."""


def clamp(value: int | None, default: int, maximum: int) -> int:
    if value is None:
        return default
    return max(1, min(int(value), maximum))


def parse_date(value: str | None, *, end: bool = False) -> datetime | None:
    """ISO date or date-time; naive values are UTC. A bare date used as an end means the whole day."""
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            result = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            return result + timedelta(days=1) if end else result
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ReaderError(f"cannot parse date {value!r}; use ISO format like 2026-01-31 or 2026-01-31T18:00") from None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def _entity_map(result) -> dict[int, Any]:
    return {utils.get_peer_id(x): x for x in itertools.chain(result.users, result.chats)}


def _muted(dialog) -> bool:
    settings = dialog.dialog.notify_settings
    until = getattr(settings, "mute_until", None)
    return bool(until and until > datetime.now(timezone.utc))


class TelegramReader:
    def __init__(
        self,
        client,
        *,
        max_response_chars: int = 60_000,
        dialog_cache_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.max_response_chars = max_response_chars
        self.dialog_cache_seconds = dialog_cache_seconds
        self._clock = clock
        self._dialogs: list | None = None
        self._dialogs_at = 0.0
        self._me = None

    # Entities and dialogs

    async def me(self):
        if self._me is None:
            self._me = await self.client.get_me()
        return self._me

    async def dialogs(self) -> list:
        """All dialogs (main list and archive), cached for `dialog_cache_seconds`."""
        now = self._clock()
        if self._dialogs is None or now - self._dialogs_at > self.dialog_cache_seconds:
            self._dialogs = [d async for d in self.client.iter_dialogs(ignore_migrated=True)]
            self._dialogs_at = now
        return self._dialogs

    async def resolve(self, chat: str | int):
        """Chat by numeric id, @username / t.me link, or exact title. Never joins anything."""
        ref = str(chat).strip()
        if not ref:
            raise ReaderError("chat must not be empty")
        if ref.lower() in ("me", "self", "saved"):
            return await self.me()
        dialogs = await self.dialogs()
        if re.fullmatch(r"-?\d+", ref):
            wanted = int(ref)
            for dialog in dialogs:
                if dialog.id == wanted:
                    return dialog.entity
            try:
                return await self.client.get_entity(wanted)
            except (ValueError, TypeError):
                raise ReaderError(f"chat id {wanted} is not among your dialogs or known chats") from None
        link = _LINK.sub("", ref)
        if link != ref or ref.startswith("@"):
            name = link.lstrip("@").split("/")[0].split("?")[0]
            if name.startswith("+") or name.lower() == "joinchat":
                raise ReaderError("invite links are not supported: the server never joins chats")
            return await self._resolve_username(name, dialogs)
        exact = [d for d in dialogs if d.name == ref]
        if not exact:
            exact = [d for d in dialogs if (d.name or "").casefold() == ref.casefold()]
        if len(exact) == 1:
            return exact[0].entity
        if len(exact) > 1:
            options = ", ".join(f"{d.name!r} (id {d.id})" for d in exact[:10])
            raise ReaderError(f"several chats are called {ref!r}: {options}; use the numeric id")
        if _USERNAME.fullmatch(ref):
            try:
                return await self._resolve_username(ref, dialogs)
            except ReaderError:
                raise ReaderError(f"no chat with the title {ref!r} and no username @{ref}; "
                                  "use list_dialogs to find its id") from None
        raise ReaderError(f"no chat with the title {ref!r}; use list_dialogs to find its id")

    async def _resolve_username(self, name: str, dialogs: list):
        if not _USERNAME.fullmatch(name):
            raise ReaderError(f"{name!r} is not a valid username")
        for dialog in dialogs:
            if name.casefold() in (u.casefold() for u in usernames(dialog.entity)):
                return dialog.entity
        try:
            result = await self.client(functions.contacts.ResolveUsernameRequest(name))
        except Exception as exc:
            raise ReaderError(f"cannot resolve @{name}: {exc}") from exc
        entity = _entity_map(result).get(utils.get_peer_id(result.peer))
        if entity is None:
            raise ReaderError(f"cannot resolve @{name}")
        return entity

    def _dialog_facts(self, dialog) -> DialogFacts:
        entity = dialog.entity
        return DialogFacts(
            peer_id=dialog.id,
            kind=chat_kind(entity),
            is_contact=isinstance(entity, types.User) and bool(entity.contact or entity.is_self),
            muted=_muted(dialog),
            unread=dialog.unread_count > 0 or bool(dialog.dialog.unread_mark),
            unread_mentions=dialog.unread_mentions_count > 0,
            archived=dialog.folder_id == ARCHIVE_FOLDER_ID,
        )

    def _dialog_dict(self, dialog) -> dict[str, Any]:
        entity = dialog.entity
        names = usernames(entity)
        data = {
            "id": dialog.id,
            "name": dialog.name,
            "type": chat_kind(entity),
            "username": names[0] if names else None,
            "unread_count": dialog.unread_count,
            "unread_mentions_count": dialog.unread_mentions_count,
            "unread_reactions_count": dialog.unread_reactions_count,
            "marked_unread": bool(dialog.dialog.unread_mark),
            "muted": _muted(dialog),
            "archived": dialog.folder_id == ARCHIVE_FOLDER_ID,
            "pinned": dialog.pinned,
            "forum": bool(getattr(entity, "forum", False)),
            "last_message_date": iso(dialog.date),
            "last_message_id": dialog.dialog.top_message,
        }
        if isinstance(entity, types.User) and entity.is_self:
            data["saved_messages"] = True
        return data

    # Tools

    async def list_dialogs(
        self,
        *,
        chat_type: str | None = None,
        archived: bool | None = None,
        unread_only: bool = False,
        name_contains: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if chat_type is not None and chat_type not in CHAT_TYPES:
            raise ReaderError(f"chat_type must be one of {', '.join(CHAT_TYPES)}")
        limit = clamp(limit, 50, MAX_DIALOG_LIMIT)
        needle = (name_contains or "").casefold()
        selected = []
        for dialog in await self.dialogs():
            facts = self._dialog_facts(dialog)
            if chat_type is not None and facts.kind != chat_type:
                continue
            if archived is not None and facts.archived != archived:
                continue
            if unread_only and not (facts.unread or facts.unread_mentions):
                continue
            if needle and needle not in (dialog.name or "").casefold() and not any(
                needle in u.casefold() for u in usernames(dialog.entity)
            ):
                continue
            selected.append(dialog)
        return self._page([self._dialog_dict(d) for d in selected], "dialogs", limit, offset)

    def _page(self, items: list, key: str, limit: int, offset: int) -> dict[str, Any]:
        offset = max(0, int(offset or 0))
        page = items[offset:offset + limit]
        more = offset + len(page) < len(items)
        return {key: page, "total": len(items), "offset": offset, "next_offset": offset + len(page) if more else None}

    async def _folders(self) -> list[Folder]:
        result = await self.client(functions.messages.GetDialogFiltersRequest())
        raw = getattr(result, "filters", result)
        me = await self.me()
        return [parse_filter(item, me.id) for item in raw]

    async def list_folders(self) -> dict[str, Any]:
        names = {d.id: d.name for d in await self.dialogs()}

        def peers(ids):
            return [{"id": i, "name": names.get(i)} for i in ids]

        folders = []
        for folder in await self._folders():
            item: dict[str, Any] = {"id": folder.id, "title": folder.title, "kind": folder.kind}
            if folder.kind != "default":
                item.update(
                    emoticon=folder.emoticon,
                    pinned_peers=peers(folder.pinned),
                    include_peers=peers(folder.include),
                )
            if folder.kind == "filter":
                item.update(flags=folder.flags(), exclude_peers=peers(folder.exclude))
            folders.append(item)
        return {"folders": folders}

    async def list_folder_chats(self, folder_id: int, *, limit: int | None = 50, offset: int = 0) -> dict[str, Any]:
        folders = {f.id: f for f in await self._folders()}
        folder = folders.get(folder_id)
        if folder is None and folder_id == 0:
            folder = Folder(id=0, title="All chats", kind="default", emoticon=None, pinned=(), include=(), exclude=())
        if folder is None:
            raise ReaderError(f"no folder with id {folder_id}; use list_folders")
        dialogs = [d for d in await self.dialogs() if folder.contains(self._dialog_facts(d))]
        # Pinned chats of the folder come first, in the folder's order.
        order = {peer: index for index, peer in enumerate(folder.pinned)}
        dialogs.sort(key=lambda d: order.get(d.id, len(order)))
        result = self._page([self._dialog_dict(d) for d in dialogs], "chats", clamp(limit, 50, MAX_DIALOG_LIMIT), offset)
        result["folder"] = {"id": folder.id, "title": folder.title, "kind": folder.kind}
        return result

    async def chat_info(self, chat: str | int) -> dict[str, Any]:
        entity = await self.resolve(chat)
        info: dict[str, Any] = dict(entity_ref(entity))
        info["usernames"] = usernames(entity)
        if isinstance(entity, types.User):
            full = (await self.client(functions.users.GetFullUserRequest(entity))).full_user
            info.update(
                first_name=entity.first_name,
                last_name=entity.last_name,
                phone=entity.phone,
                contact=bool(entity.contact),
                mutual_contact=bool(entity.mutual_contact),
                is_self=bool(entity.is_self),
                deleted=bool(entity.deleted),
                verified=bool(entity.verified),
                about=full.about,
                common_chats_count=full.common_chats_count,
                pinned_message_id=full.pinned_msg_id,
            )
        elif isinstance(entity, types.Channel):
            response = await self.client(functions.channels.GetFullChannelRequest(entity))
            full = response.full_chat
            linked = _entity_map(response).get(utils.get_peer_id(types.PeerChannel(full.linked_chat_id))) \
                if full.linked_chat_id else None
            info.update(
                about=full.about,
                members_count=full.participants_count if full.participants_count is not None
                else entity.participants_count,
                forum=bool(entity.forum),
                broadcast=bool(entity.broadcast),
                megagroup=bool(entity.megagroup),
                verified=bool(entity.verified),
                scam=bool(entity.scam),
                created=iso(entity.date),
                linked_chat=entity_ref(linked) if linked is not None else (
                    {"id": utils.get_peer_id(types.PeerChannel(full.linked_chat_id))} if full.linked_chat_id else None
                ),
                slowmode_seconds=full.slowmode_seconds,
                pinned_message_id=full.pinned_msg_id,
                left=bool(entity.left),
            )
        elif isinstance(entity, types.Chat):
            full = (await self.client(functions.messages.GetFullChatRequest(entity.id))).full_chat
            participants = getattr(full.participants, "participants", None)
            info.update(
                about=full.about,
                members_count=len(participants) if participants is not None else entity.participants_count,
                forum=False,
                created=iso(entity.date),
                pinned_message_id=full.pinned_msg_id,
                migrated_to=utils.get_peer_id(types.PeerChannel(entity.migrated_to.channel_id))
                if isinstance(entity.migrated_to, types.InputChannel) else None,
            )
        for dialog in await self.dialogs():
            if dialog.id == info["id"]:
                info["dialog"] = self._dialog_dict(dialog)
                break
        return info

    async def list_forum_topics(
        self, chat: str | int, *, query: str | None = None, limit: int | None = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        entity = await self.resolve(chat)
        if not getattr(entity, "forum", False):
            raise ReaderError(f"{utils.get_display_name(entity)!r} is not a forum group")
        offset_date, offset_id, offset_topic = None, 0, 0
        if cursor:
            try:
                stamp, offset_id, offset_topic = (int(x) for x in cursor.split(":"))
            except ValueError:
                raise ReaderError("invalid cursor; pass next_cursor from the previous call") from None
            offset_date = datetime.fromtimestamp(stamp, timezone.utc) if stamp else None
        limit = clamp(limit, 50, MAX_LIMIT)
        result = await self.client(functions.messages.GetForumTopicsRequest(
            peer=utils.get_input_peer(entity), offset_date=offset_date, offset_id=offset_id,
            offset_topic=offset_topic, limit=limit, q=query or None,
        ))
        tops = {m.id: m for m in result.messages}
        topics = []
        for topic in result.topics:
            if not isinstance(topic, types.ForumTopic):
                continue
            last = tops.get(topic.top_message)
            topics.append({
                "id": topic.id,
                "title": topic.title,
                "created": iso(topic.date),
                "closed": bool(topic.closed),
                "pinned": bool(topic.pinned),
                "hidden": bool(topic.hidden),
                "unread_count": topic.unread_count,
                "unread_mentions_count": topic.unread_mentions_count,
                "last_message_id": topic.top_message,
                "last_message_date": iso(getattr(last, "date", None)),
            })
        next_cursor = None
        if len(result.topics) >= limit and topics:
            last_topic = result.topics[-1]
            last_date = getattr(tops.get(last_topic.top_message), "date", None) or last_topic.date
            next_cursor = f"{int(last_date.timestamp()) if last_date else 0}:{last_topic.top_message}:{last_topic.id}"
        return {"chat": entity_ref(entity), "count": result.count, "topics": topics, "next_cursor": next_cursor}

    def _collect(self, messages, limit: int, max_text: int, entities=None) -> tuple[list[dict], bool]:
        """Serialize up to `limit` messages within the response size budget; returns (items, cut_by_size)."""
        items, used = [], 0
        for message in messages[:limit]:
            item = message_dict(message, max_text=max_text, entities=entities)
            size = len(json.dumps(item, ensure_ascii=False))
            if items and used + size > self.max_response_chars:
                return items, True
            items.append(item)
            used += size
        return items, False

    async def read_messages(
        self,
        chat: str | int,
        *,
        limit: int | None = 50,
        offset_id: int = 0,
        min_id: int = 0,
        max_id: int = 0,
        since: str | None = None,
        until: str | None = None,
        topic_id: int | None = None,
        reply_to: int | None = None,
        oldest_first: bool = False,
        max_text: int | None = MAX_TEXT,
    ) -> dict[str, Any]:
        if topic_id and reply_to:
            raise ReaderError("pass either topic_id or reply_to, not both")
        entity = await self.resolve(chat)
        limit = clamp(limit, 50, MAX_LIMIT)
        max_text = clamp(max_text, MAX_TEXT, MAX_TEXT * 16)
        start, end = parse_date(since), parse_date(until, end=True)
        kwargs: dict[str, Any] = {"min_id": min_id or 0, "max_id": max_id or 0, "offset_id": offset_id or 0}
        if oldest_first:
            kwargs["reverse"] = True
            if not offset_id and start:
                kwargs["offset_date"] = start
        elif not offset_id and end:
            kwargs["offset_date"] = end
        messages, scanned, passed_range = [], 0, False
        async for message in self.client.iter_messages(
            entity, limit=limit + 1, reply_to=topic_id or reply_to or None, **kwargs
        ):
            scanned += 1
            before, after = bool(start and message.date < start), bool(end and message.date >= end)
            if (after if oldest_first else before):
                passed_range = True  # every following message is outside the range too
                break
            if before or after:
                continue
            messages.append(message)
        items, cut = self._collect(messages, limit, max_text)
        more = cut or len(messages) > limit or (not passed_range and scanned > limit)
        return {
            "chat": entity_ref(entity),
            "messages": items,
            "order": "oldest_first" if oldest_first else "newest_first",
            "has_more": more,
            "next_offset_id": items[-1]["id"] if more and items else None,
            "truncated_by_size": cut,
        }

    async def get_messages(self, chat: str | int, ids: list[int], *, max_text: int | None = MAX_TEXT) -> dict[str, Any]:
        if not ids:
            raise ReaderError("ids must not be empty")
        if len(ids) > MAX_LIMIT:
            raise ReaderError(f"at most {MAX_LIMIT} ids per call")
        entity = await self.resolve(chat)
        found = await self.client.get_messages(entity, ids=list(ids))
        present = [m for m in found if m is not None and not isinstance(m, types.MessageEmpty)]
        items, _ = self._collect(present, len(present), clamp(max_text, MAX_TEXT, MAX_TEXT * 16))
        returned = {item["id"] for item in items}
        found_ids = {m.id for m in present}
        return {
            "chat": entity_ref(entity),
            "messages": items,
            "missing_ids": [i for i in ids if i not in found_ids],
            "omitted_for_size": [m.id for m in present if m.id not in returned],
        }

    def _media_filter(self, media: str | None):
        if media is None:
            return types.InputMessagesFilterEmpty()
        if media not in MEDIA_FILTERS:
            raise ReaderError(f"media must be one of {', '.join(MEDIA_FILTERS)}")
        return MEDIA_FILTERS[media]()

    async def search_messages(
        self,
        chat: str | int,
        *,
        query: str = "",
        sender: str | int | None = None,
        since: str | None = None,
        until: str | None = None,
        media: str | None = None,
        topic_id: int | None = None,
        limit: int | None = 50,
        offset_id: int = 0,
        max_text: int | None = MAX_TEXT,
    ) -> dict[str, Any]:
        entity = await self.resolve(chat)
        from_entity = await self.resolve(sender) if sender not in (None, "") else None
        limit = clamp(limit, 50, MAX_LIMIT)
        result = await self.client(functions.messages.SearchRequest(
            peer=utils.get_input_peer(entity),
            q=query or "",
            filter=self._media_filter(media),
            min_date=parse_date(since),
            max_date=parse_date(until, end=True),
            offset_id=offset_id or 0,
            add_offset=0,
            limit=limit,
            max_id=0,
            min_id=0,
            hash=0,
            from_id=utils.get_input_peer(from_entity) if from_entity is not None else None,
            top_msg_id=topic_id or None,
        ))
        messages = [m for m in result.messages if not isinstance(m, types.MessageEmpty)]
        if from_entity is not None:
            # Telegram ignores from_id in private chats, so check it here as well.
            wanted = utils.get_peer_id(from_entity)
            messages = [m for m in messages if m.sender_id == wanted]
        items, cut = self._collect(messages, limit, clamp(max_text, MAX_TEXT, MAX_TEXT * 16), _entity_map(result))
        more = cut or len(result.messages) >= limit
        return {
            "chat": entity_ref(entity),
            "total": getattr(result, "count", len(result.messages)),
            "messages": items,
            "has_more": more,
            "next_offset_id": items[-1]["id"] if more and items else None,
            "truncated_by_size": cut,
        }

    async def search_global(
        self,
        query: str,
        *,
        since: str | None = None,
        until: str | None = None,
        media: str | None = None,
        chat_type: str | None = None,
        limit: int | None = 50,
        cursor: str | None = None,
        max_text: int | None = MAX_TEXT,
    ) -> dict[str, Any]:
        if not (query or "").strip() and media is None:
            raise ReaderError("global search needs a query or a media filter")
        only = {None: {}, "user": {"users_only": True}, "group": {"groups_only": True},
                "channel": {"broadcasts_only": True}}
        if chat_type not in only:
            raise ReaderError("chat_type must be user, group or channel")
        offset_rate, offset_peer, offset_id = 0, types.InputPeerEmpty(), 0
        if cursor:
            try:
                rate, peer, offset_id = (int(x) for x in cursor.split(":"))
            except ValueError:
                raise ReaderError("invalid cursor; pass next_cursor from the previous call") from None
            offset_rate = rate
            try:
                offset_peer = await self.client.get_input_entity(peer)
            except (ValueError, TypeError):
                raise ReaderError("the cursor refers to an unknown chat; start the search again") from None
        limit = clamp(limit, 50, MAX_LIMIT)
        result = await self.client(functions.messages.SearchGlobalRequest(
            q=query or "",
            filter=self._media_filter(media),
            min_date=parse_date(since),
            max_date=parse_date(until, end=True),
            offset_rate=offset_rate,
            offset_peer=offset_peer,
            offset_id=offset_id,
            limit=limit,
            **only[chat_type],
        ))
        entities = _entity_map(result)
        messages = [m for m in result.messages if not isinstance(m, types.MessageEmpty)]
        items, cut = self._collect(messages, limit, clamp(max_text, MAX_TEXT, MAX_TEXT * 16), entities)
        for item in items:
            item["chat"] = entity_ref(entities.get(item["chat_id"])) or {"id": item["chat_id"]}
        next_cursor = None
        if items and (cut or len(result.messages) >= limit):
            last = messages[len(items) - 1]
            rate = getattr(result, "next_rate", None) if not cut else None
            next_cursor = f"{rate if rate is not None else int(last.date.timestamp())}:" \
                          f"{utils.get_peer_id(last.peer_id)}:{last.id}"
        return {
            "total": getattr(result, "count", len(result.messages)),
            "messages": items,
            "next_cursor": next_cursor,
            "truncated_by_size": cut,
        }
