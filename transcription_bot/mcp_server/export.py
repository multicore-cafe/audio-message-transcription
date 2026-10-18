"""Export of a chat (or a date range / topic of it) to JSON and Markdown files."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import types

from .reader import ReaderError, TelegramReader, parse_date
from .serialize import entity_ref, iso, message_dict

FORMATS = ("json", "markdown", "both")
DEFAULT_MAX_MESSAGES = 10_000


def _slug(name: str) -> str:
    slug = re.sub(r"[^\w.-]+", "_", name or "", flags=re.UNICODE).strip("._")
    return slug[:50] or "chat"


def _sender_name(item: dict[str, Any]) -> str:
    sender = item.get("sender") or {}
    return sender.get("name") or (f"@{sender['username']}" if sender.get("username") else f"id {sender.get('id')}")


def markdown(chat: dict[str, Any], meta: dict[str, Any], items: list[dict[str, Any]]) -> str:
    lines = [
        f"# {chat.get('name') or chat['id']}",
        "",
        f"- Chat id: {chat['id']}" + (f" (@{chat['username']})" if chat.get("username") else ""),
        f"- Type: {chat.get('type')}",
        f"- Exported: {meta['exported_at']}",
        f"- Range: {meta['since'] or 'beginning'} – {meta['until'] or 'now'}"
        + (f", topic {meta['topic_id']}" if meta.get("topic_id") else ""),
        f"- Messages: {meta['message_count']}" + (" (limit reached, export is incomplete)" if meta["truncated"] else ""),
    ]
    day = None
    for item in items:
        stamp = item.get("date") or ""
        if stamp[:10] != day:
            day = stamp[:10]
            lines += ["", f"## {day}"]
        details = [f"#{item['id']}"]
        if item.get("reply_to", {}).get("message_id"):
            details.append(f"reply to #{item['reply_to']['message_id']}")
        forward = item.get("forward")
        if forward:
            source = (forward.get("from") or {}).get("name") or forward.get("from_name") or "unknown"
            details.append(f"forwarded from {source}")
        if item.get("edit_date"):
            details.append("edited")
        lines += ["", f"**{stamp[11:19]} · {_sender_name(item)}** ({', '.join(details)})"]
        if item.get("action"):
            lines.append(f"_service message: {item['action']}_")
        if item.get("text"):
            lines.append(item["text"])
        media = item.get("media")
        if media:
            label = media.get("type", "media")
            if media.get("file"):
                lines.append(f"_[{label}: [{media['file']}]({media['file']})]_")
            else:
                extra = media.get("file_name") or media.get("url") or media.get("question") or ""
                lines.append(f"_[{label}{': ' + extra if extra else ''}]_")
        if item.get("reactions"):
            lines.append("Reactions: " + ", ".join(f"{r['reaction']} {r['count']}" for r in item["reactions"]))
    return "\n".join(lines) + "\n"


class Exporter:
    def __init__(
        self,
        reader: TelegramReader,
        export_dir: Path,
        host_dir: Path,
        *,
        max_messages: int = 50_000,
        media_max_bytes: int = 50 * 1024 * 1024,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.reader = reader
        self.export_dir = Path(export_dir)
        self.host_dir = Path(host_dir)
        self.max_messages = max_messages
        self.media_max_bytes = media_max_bytes
        self._now = now

    async def export_chat(
        self,
        chat: str | int,
        *,
        since: str | None = None,
        until: str | None = None,
        topic_id: int | None = None,
        format: str = "both",
        include_media: bool = False,
        max_messages: int | None = None,
    ) -> dict[str, Any]:
        if format not in FORMATS:
            raise ReaderError(f"format must be one of {', '.join(FORMATS)}")
        limit = max(1, min(max_messages or DEFAULT_MAX_MESSAGES, self.max_messages))
        client = self.reader.client
        entity = await self.reader.resolve(chat)
        chat_ref = entity_ref(entity)
        start, end = parse_date(since), parse_date(until, end=True)
        exported_at = self._now()

        base = f"{_slug(chat_ref['name'] or '')}_{abs(chat_ref['id'])}_{exported_at:%Y%m%d-%H%M%S}"
        for attempt in range(1, 100):
            folder = base if attempt == 1 else f"{base}-{attempt}"
            target = self.export_dir / folder
            try:
                target.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                if not self.export_dir.is_dir():
                    raise ReaderError(f"the export directory {self.export_dir} is not a directory "
                                      "(it must exist and be writable)") from None
            except OSError as exc:
                raise ReaderError(f"cannot create the export folder {target}: {exc} "
                                  "(the export directory must exist and be writable)") from exc
        else:
            raise ReaderError(f"too many exports of this chat in one second in {self.export_dir}")

        items: list[dict[str, Any]] = []
        truncated = False
        media_files = media_skipped = 0
        kwargs: dict[str, Any] = {"reverse": True}
        if start:
            kwargs["offset_date"] = start
        async for message in client.iter_messages(entity, limit=limit + 1, reply_to=topic_id or None, **kwargs):
            if end and message.date >= end:
                break
            if start and message.date < start:
                continue
            if len(items) >= limit:
                truncated = True
                break
            item = message_dict(message)
            if include_media and item.get("media") and message.file is not None \
                    and not isinstance(message.media, types.MessageMediaWebPage):
                size = message.file.size
                if size is not None and size > self.media_max_bytes:
                    item["media"]["skipped"] = f"larger than {self.media_max_bytes // (1024 * 1024)} MB"
                    media_skipped += 1
                else:
                    media_dir = target / "media"
                    media_dir.mkdir(exist_ok=True)
                    name = f"{message.id}{message.file.ext or ''}"
                    saved = await client.download_media(message, file=str(media_dir / name))
                    if saved:
                        item["media"]["file"] = f"media/{Path(saved).name}"
                        media_files += 1
            items.append(item)

        meta = {
            "exported_at": iso(exported_at),
            "since": iso(start),
            "until": iso(end),
            "topic_id": topic_id,
            "message_count": len(items),
            "truncated": truncated,
        }
        files = {}
        if format in ("json", "both"):
            path = target / "messages.json"
            path.write_text(json.dumps({"chat": chat_ref, **meta, "messages": items}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            files["json"] = str(self.host_dir / folder / path.name)
        if format in ("markdown", "both"):
            path = target / "messages.md"
            path.write_text(markdown(chat_ref, meta, items), encoding="utf-8")
            files["markdown"] = str(self.host_dir / folder / path.name)
        return {
            "chat": chat_ref,
            "message_count": len(items),
            "truncated": truncated,
            "media_files": media_files,
            "media_skipped": media_skipped,
            "directory": str(self.host_dir / folder),
            "files": files,
            "first_message_date": items[0]["date"] if items else None,
            "last_message_date": items[-1]["date"] if items else None,
        }

