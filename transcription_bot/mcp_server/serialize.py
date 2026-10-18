"""JSON-friendly views of Telethon objects."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from telethon import types, utils


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def chat_kind(entity) -> str:
    """"user", "bot", "group" (basic groups and supergroups) or "channel" (broadcast channels)."""
    if isinstance(entity, types.User):
        return "bot" if entity.bot else "user"
    if isinstance(entity, types.Channel) and not entity.megagroup and not entity.gigagroup:
        return "channel"
    return "group"


def usernames(entity) -> list[str]:
    names = [entity.username] if getattr(entity, "username", None) else []
    for extra in getattr(entity, "usernames", None) or ():
        if extra.active and extra.username not in names:
            names.append(extra.username)
    return names


def entity_ref(entity) -> dict[str, Any] | None:
    if entity is None:
        return None
    names = usernames(entity)
    return {
        "id": utils.get_peer_id(entity),
        "name": utils.get_display_name(entity) or None,
        "username": names[0] if names else None,
        "type": chat_kind(entity),
    }


def peer_id(peer) -> int | None:
    return utils.get_peer_id(peer) if peer is not None else None


def _media(message) -> dict[str, Any] | None:
    media = message.media
    if media is None:
        return None
    info: dict[str, Any] = {}
    if isinstance(media, types.MessageMediaPhoto):
        info["type"] = "photo"
    elif isinstance(media, types.MessageMediaDocument):
        if message.voice:
            info["type"] = "voice"
        elif message.video_note:
            info["type"] = "video_note"
        elif message.gif:
            info["type"] = "gif"
        elif message.sticker:
            info["type"] = "sticker"
        elif message.video:
            info["type"] = "video"
        elif message.audio:
            info["type"] = "audio"
        else:
            info["type"] = "document"
    elif isinstance(media, types.MessageMediaWebPage):
        info["type"] = "webpage"
        page = media.webpage
        if isinstance(page, types.WebPage):
            info.update(url=page.url, title=page.title)
        return info
    elif isinstance(media, types.MessageMediaPoll):
        question = media.poll.question
        info.update(
            type="poll",
            question=question.text if isinstance(question, types.TextWithEntities) else question,
            answers=[a.text.text if isinstance(a.text, types.TextWithEntities) else a.text for a in media.poll.answers],
        )
        return info
    elif isinstance(media, (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue)):
        info["type"] = "location"
        geo = media.geo
        if isinstance(geo, types.GeoPoint):
            info.update(lat=geo.lat, long=geo.long)
        if isinstance(media, types.MessageMediaVenue):
            info.update(title=media.title, address=media.address)
        return info
    elif isinstance(media, types.MessageMediaContact):
        info.update(type="contact", name=f"{media.first_name} {media.last_name}".strip(), phone=media.phone_number)
        return info
    else:
        name = type(media).__name__.removeprefix("MessageMedia")
        info["type"] = "".join("_" + c.lower() if c.isupper() else c for c in name).lstrip("_") or "unknown"
        return info
    file = message.file
    if file is not None:
        info.update(
            file_name=file.name,
            mime_type=file.mime_type,
            size=file.size,
            duration=file.duration,
        )
    return {key: value for key, value in info.items() if value is not None}


def _reactions(message) -> list[dict[str, Any]]:
    reactions = getattr(message, "reactions", None)
    result = []
    for item in getattr(reactions, "results", None) or ():
        reaction = item.reaction
        if isinstance(reaction, types.ReactionEmoji):
            label = reaction.emoticon
        elif isinstance(reaction, types.ReactionCustomEmoji):
            label = f"custom:{reaction.document_id}"
        elif isinstance(reaction, types.ReactionPaid):
            label = "paid"
        else:
            label = type(reaction).__name__
        result.append({"reaction": label, "count": item.count})
    return result


def _forward(message, entities: dict[int, Any]) -> dict[str, Any] | None:
    header = message.fwd_from
    if header is None:
        return None
    source = entities.get(peer_id(header.from_id))
    forward = getattr(message, "forward", None)
    if source is None and forward is not None:
        source = forward.sender or forward.chat
    return {
        "from": entity_ref(source) if source is not None else (
            {"id": peer_id(header.from_id)} if header.from_id is not None else None
        ),
        "from_name": header.from_name,
        "date": iso(header.date),
        "channel_post": header.channel_post,
        "post_author": header.post_author,
    }


def _reply(message) -> dict[str, Any] | None:
    header = message.reply_to
    if not isinstance(header, types.MessageReplyHeader):
        return None
    reply = {
        "message_id": header.reply_to_msg_id,
        "top_id": header.reply_to_top_id,
        "forum_topic": bool(header.forum_topic) or None,
        "peer_id": peer_id(header.reply_to_peer_id),
        "quote": header.quote_text,
    }
    return {key: value for key, value in reply.items() if value is not None}


def topic_id(message) -> int | None:
    """Forum topic of a message (`None` outside forums; 1 is the General topic)."""
    header = message.reply_to
    if isinstance(header, types.MessageReplyHeader) and header.forum_topic:
        return header.reply_to_top_id or header.reply_to_msg_id
    return None


def message_dict(
    message, *, max_text: int | None = None, entities: dict[int, Any] | None = None
) -> dict[str, Any]:
    """One message; text longer than `max_text` characters is cut and marked `text_truncated`.

    `entities` maps peer ids to users and chats of a raw API response, for sender names.
    """
    if isinstance(message, types.MessageEmpty):
        return {"id": message.id, "empty": True}
    entities = entities or {}
    text = message.message or ""
    sender = entities.get(message.sender_id) or getattr(message, "sender", None)
    data: dict[str, Any] = {
        "id": message.id,
        "chat_id": peer_id(message.peer_id),
        "date": iso(message.date),
        "sender": entity_ref(sender) if sender is not None else (
            {"id": message.sender_id} if message.sender_id is not None else None
        ),
        "out": bool(message.out),
    }
    if isinstance(message, types.MessageService):
        data["action"] = type(message.action).__name__.removeprefix("MessageAction")
        return data
    if max_text is not None and len(text) > max_text:
        data["text"] = text[:max_text]
        data["text_truncated"] = len(text)
    else:
        data["text"] = text
    optional = {
        "media": _media(message),
        "reply_to": _reply(message),
        "topic_id": topic_id(message),
        "forward": _forward(message, entities),
        "reactions": _reactions(message) or None,
        "views": message.views,
        "forwards": message.forwards,
        "replies": message.replies.replies if message.replies is not None else None,
        "edit_date": iso(message.edit_date),
        "grouped_id": message.grouped_id,
        "post_author": message.post_author,
        "pinned": True if message.pinned else None,
    }
    data.update({key: value for key, value in optional.items() if value is not None})
    return data
