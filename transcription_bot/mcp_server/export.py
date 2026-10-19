"""Export of a chat (or a date range / topic of it) to JSON and Markdown files."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import types

from .reader import MAX_LIMIT, MEDIA_FILTERS, ReaderError, TelegramReader, _entity_map, global_cursor, parse_date
from .serialize import entity_ref, iso, message_dict

FORMATS = ("json", "markdown", "both")
DEFAULT_MAX_MESSAGES = 10_000
DEFAULT_SEARCH_MESSAGES = 1_000
# One call stops after this long and returns a partial export (MCP clients time out tool calls).
TIME_BUDGET = 50


def _slug(name: str) -> str:
    slug = re.sub(r"[^\w.-]+", "_", name or "", flags=re.UNICODE).strip("._")
    return slug[:50] or "chat"


def _sender_name(item: dict[str, Any]) -> str:
    sender = item.get("sender") or {}
    return sender.get("name") or (f"@{sender['username']}" if sender.get("username") else f"id {sender.get('id')}")


def _markdown_messages(items: list[dict[str, Any]], *, show_chat: bool = False) -> list[str]:
    """Day headings and one block per message, shared by the chat and search exporters."""
    lines: list[str] = []
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
        who = _sender_name(item)
        if show_chat:
            chat = item.get("chat") or {}
            who = f"{chat.get('name') or chat.get('id', '?')} · {who}"
        lines += ["", f"**{stamp[11:19]} · {who}** ({', '.join(details)})"]
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
    return lines


def markdown(chat: dict[str, Any], meta: dict[str, Any], items: list[dict[str, Any]]) -> str:
    lines = [
        f"# {chat.get('name') or chat['id']}",
        "",
        f"- Chat id: {chat['id']}" + (f" (@{chat['username']})" if chat.get("username") else ""),
        f"- Type: {chat.get('type')}",
        f"- Exported: {meta['exported_at']}",
        f"- Range: {meta['since'] or 'beginning'} – {meta['until'] or 'now'}"
        + (f", topic {meta['topic_id']}" if meta.get("topic_id") else ""),
        f"- Messages: {meta['message_count']}" + (
            f" (export is incomplete: {meta['truncated_reason']}; continue with after_id={meta['resume_after_id']})"
            if meta["truncated"] else ""),
    ]
    return "\n".join(lines + _markdown_messages(items)) + "\n"


def search_markdown(search: dict[str, Any], meta: dict[str, Any], items: list[dict[str, Any]]) -> str:
    """Header with the search parameters plus the messages of one part."""
    chat = search.get("chat")
    title = (chat or {}).get("name") or search.get("query") or "all chats"
    lines = [f"# Telegram search: {title}", ""]
    if chat:
        lines.append(f"- Chat: {chat.get('name') or chat['id']} (id {chat['id']})")
    if search.get("chat_type"):
        lines.append(f"- Chat type: {search['chat_type']}")
    lines.append(f"- Query: {search['query']}" if search.get("query") else "- Query: (none)")
    if search.get("sender"):
        lines.append(f"- Sender: {search['sender']}")
    lines.append(f"- Range: {search.get('since') or 'beginning'} – {search.get('until') or 'now'}")
    if search.get("media"):
        lines.append(f"- Media: {search['media']}")
    if search.get("topic_id"):
        lines.append(f"- Topic: {search['topic_id']}")
    lines += [
        f"- Messages: {meta['message_count']} in this part, {meta['total_messages']} in total "
        f"from {meta['chats_count']} chats",
        f"- Part {meta['part']} written {meta['exported_at']}" + (
            f" (export is incomplete: {meta['truncated_reason']}; continue with resume={meta['resume']})"
            if meta["truncated"] else ""),
    ]
    return "\n".join(lines + _markdown_messages(items, show_chat=True)) + "\n"


class Exporter:
    def __init__(
        self,
        reader: TelegramReader,
        export_dir: Path,
        host_dir: Path,
        *,
        max_messages: int = 50_000,
        media_max_bytes: int = 50 * 1024 * 1024,
        media_max_total_bytes: int = 1000 * 1024 * 1024,
        time_budget: float = TIME_BUDGET,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        clock: Callable[[], float] = time.monotonic,
    ):
        self.reader = reader
        self.export_dir = Path(export_dir)
        self.host_dir = Path(host_dir)
        self.max_messages = max_messages
        self.media_max_bytes = media_max_bytes
        self.media_max_total_bytes = media_max_total_bytes
        self.time_budget = time_budget
        self._now = now
        self._clock = clock

    def _folder(self, base: str) -> Path:
        """Create a fresh export folder (base, base-2, ... ) inside the export directory."""
        for attempt in range(1, 100):
            name = base if attempt == 1 else f"{base}-{attempt}"
            target = self.export_dir / name
            try:
                target.mkdir(parents=True, exist_ok=False)
                return target
            except FileExistsError:
                if not self.export_dir.is_dir():
                    raise ReaderError(f"the export directory {self.export_dir} is not a directory "
                                      "(it must exist and be writable)") from None
            except OSError as exc:
                raise ReaderError(f"cannot create the export folder {target}: {exc} "
                                  "(the export directory must exist and be writable)") from exc
        raise ReaderError(f"too many exports in one second in {self.export_dir}")

    async def _save_media(self, target: Path, message, item: dict[str, Any], counters: dict[str, int]) -> None:
        """Download the message's media into the folder unless a size cap says no; updates counters."""
        if not (item.get("media") and message.file is not None
                and not isinstance(message.media, types.MessageMediaWebPage)):
            return
        size = message.file.size
        if size is not None and size > self.media_max_bytes:
            item["media"]["skipped"] = f"larger than {self.media_max_bytes // (1024 * 1024)} MB"
            counters["skipped"] += 1
        elif counters["bytes"] + (size or 0) > self.media_max_total_bytes:
            item["media"]["skipped"] = f"media of this export reached {self.media_max_total_bytes // (1024 * 1024)} MB"
            counters["skipped"] += 1
        else:
            media_dir = target / "media"
            media_dir.mkdir(exist_ok=True)
            name = f"{message.id}{message.file.ext or ''}"
            saved = await self.reader.client.download_media(message, file=str(media_dir / name))
            if saved:
                item["media"]["file"] = f"media/{Path(saved).name}"
                counters["files"] += 1
                counters["bytes"] += size if size is not None else Path(saved).stat().st_size

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
        after_id: int | None = None,
    ) -> dict[str, Any]:
        """Export messages oldest first. A partial export returns `resume_after_id` for the next call."""
        if format not in FORMATS:
            raise ReaderError(f"format must be one of {', '.join(FORMATS)}")
        limit = max(1, min(max_messages or DEFAULT_MAX_MESSAGES, self.max_messages))
        client = self.reader.client
        entity = await self.reader.resolve(chat)
        chat_ref = entity_ref(entity)
        start, end = parse_date(since), parse_date(until, end=True)
        exported_at = self._now()
        deadline = self._clock() + self.time_budget

        base = f"{_slug(chat_ref['name'] or '')}_{abs(chat_ref['id'])}_{exported_at:%Y%m%d-%H%M%S}"
        target = self._folder(base)
        folder = target.name

        items: list[dict[str, Any]] = []
        stop_reason: str | None = None  # why the export stopped early
        counters = {"files": 0, "skipped": 0, "bytes": 0}
        kwargs: dict[str, Any] = {"reverse": True}
        if after_id:
            kwargs["offset_id"] = after_id
        elif start:
            kwargs["offset_date"] = start
        async for message in client.iter_messages(entity, limit=limit + 1, reply_to=topic_id or None, **kwargs):
            if end and message.date >= end:
                break
            if start and message.date < start:
                continue
            if len(items) >= limit:
                stop_reason = "max_messages reached"
                break
            if items and self._clock() > deadline:
                stop_reason = f"time budget of {self.time_budget:g} s per call used up"
                break
            item = message_dict(message)
            if include_media:
                await self._save_media(target, message, item, counters)
            items.append(item)
        truncated = stop_reason is not None
        resume_after_id = items[-1]["id"] if truncated and items else None

        meta = {
            "exported_at": iso(exported_at),
            "since": iso(start),
            "until": iso(end),
            "topic_id": topic_id,
            "message_count": len(items),
            "truncated": truncated,
            "truncated_reason": stop_reason,
            "resume_after_id": resume_after_id,
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
            "truncated_reason": stop_reason,
            "resume_after_id": resume_after_id,
            "media_files": counters["files"],
            "media_skipped": counters["skipped"],
            "directory": str(self.host_dir / folder),
            "files": files,
            "first_message_date": items[0]["date"] if items else None,
            "last_message_date": items[-1]["date"] if items else None,
        }

    async def export_search(
        self,
        *,
        chat: str | int | None = None,
        query: str = "",
        sender: str | int | None = None,
        since: str | None = None,
        until: str | None = None,
        media: str | None = None,
        topic_id: int | None = None,
        chat_type: str | None = None,
        format: str = "both",
        include_media: bool = False,
        max_messages: int | None = None,
        resume: str | None = None,
    ) -> dict[str, Any]:
        """Search (in `chat` or across all chats) and write the matches to files, one part per call.

        The result carries only host paths, counts and the `resume` value of a partial export,
        never message content. A `resume` call continues the same directory with the next part
        file (part1, part2, ...) and must be passed alone.
        """
        if format not in FORMATS:
            raise ReaderError(f"format must be one of {', '.join(FORMATS)}")
        if resume not in (None, ""):
            given = [key for key, value in (("chat", chat), ("query", query), ("sender", sender),
                                            ("since", since), ("until", until), ("media", media),
                                            ("topic_id", topic_id), ("chat_type", chat_type))
                     if value not in (None, "")]
            if given:
                raise ReaderError(f"with resume pass no other search parameters (remove {', '.join(given)})")
            return await self._resume_search(str(resume).strip(), max_messages)
        if media is not None and media not in MEDIA_FILTERS:
            raise ReaderError(f"media must be one of {', '.join(MEDIA_FILTERS)}")
        if chat in (None, ""):
            kind, chat_ref = "global", None
            if chat_type not in (None, "user", "group", "channel"):
                raise ReaderError("chat_type must be user, group or channel")
            if sender not in (None, "") or topic_id:
                raise ReaderError("sender and topic_id need a specific chat; omit them in a global search")
        else:
            if chat_type is not None:
                raise ReaderError("chat_type applies only to a global search; omit chat to use it")
            kind = "chat"
            chat_ref = entity_ref(await self.reader.resolve(chat))
        exported_at = self._now()
        label = (query or "").strip() or (chat_ref or {}).get("name") or chat_type or "all chats"
        target = self._folder(f"search_{exported_at:%Y%m%d-%H%M%S}_{_slug(label)}")
        state: dict[str, Any] = {
            "kind": kind,
            "chat": chat if kind == "chat" else None,
            "query": query or "",
            "sender": sender or None,
            "since": since, "until": until, "media": media, "topic_id": topic_id,
            "chat_type": chat_type if kind == "global" else None,
            "format": format, "include_media": include_media,
            "chat_ref": chat_ref,
            "part": 0, "next": None, "finished": False,
            "total_messages": 0, "chats": {},
            "media_files": 0, "media_skipped": 0, "media_bytes": 0,
        }
        limit = max(1, min(max_messages or DEFAULT_SEARCH_MESSAGES, self.max_messages))
        return await self._search_part(target, state, limit, self._clock() + self.time_budget)

    async def _resume_search(self, token: str, max_messages: int | None) -> dict[str, Any]:
        """Continue a partial search export; `token` is the directory name of the first part."""
        if not token or Path(token).name != token or token in (".", ".."):
            raise ReaderError("invalid resume value; pass the resume field of a previous export_search result")
        state_path = self.export_dir / token / "search.json"
        if not state_path.is_file():
            raise ReaderError(f"no partial search export {token!r} in {self.export_dir}")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReaderError(f"cannot continue {token!r}: {exc}") from exc
        kind = state.get("kind")
        if kind not in ("chat", "global"):
            raise ReaderError(f"{token!r} is not a search export; start a new export_search")
        if state.get("finished"):
            raise ReaderError(f"{token!r} is already complete; start a new export_search")
        if not isinstance(state.get("next"), int if kind == "chat" else str):
            raise ReaderError(f"{token!r} is not a search export; start a new export_search")
        limit = max(1, min(max_messages or DEFAULT_SEARCH_MESSAGES, self.max_messages))
        return await self._search_part(self.export_dir / token, state, limit, self._clock() + self.time_budget)

    @staticmethod
    def _search_display(state: dict[str, Any]) -> dict[str, Any]:
        display = {key: state.get(key) for key in
                   ("kind", "chat_ref", "query", "sender", "since", "until", "media", "topic_id", "chat_type")}
        display["chat"] = display.pop("chat_ref")
        return display

    async def _search_part(self, target: Path, state: dict[str, Any], limit: int, deadline: float) -> dict[str, Any]:
        """Fetch search pages until the part's limit, the time budget or the end of the results."""
        kind = state["kind"]
        items: list[dict[str, Any]] = []
        counters = {"files": state["media_files"], "skipped": state["media_skipped"],
                    "bytes": state["media_bytes"]}
        next_value = state["next"]  # offset_id for a chat search, cursor for a global one
        stop_reason: str | None = None
        finished = False
        while True:
            page_size = min(MAX_LIMIT, limit - len(items))
            if kind == "chat":
                _, result, raw = await self.reader.search_chat_page(
                    state["chat"], query=state["query"], sender=state["sender"], since=state["since"],
                    until=state["until"], media=state["media"], topic_id=state["topic_id"],
                    limit=page_size, offset_id=next_value or 0)
                entities = None
            else:
                result, raw = await self.reader.search_global_page(
                    state["query"], since=state["since"], until=state["until"], media=state["media"],
                    chat_type=state["chat_type"], limit=page_size, cursor=next_value)
                entities = _entity_map(result)
            resume_from = None  # the message a continuation resumes after when cut mid-page
            for index, message in enumerate(raw):
                if items and self._clock() > deadline:
                    stop_reason = f"time budget of {self.time_budget:g} s per call used up"
                    if index:
                        resume_from = raw[index - 1]
                    break
                item = message_dict(message)
                if kind == "chat":
                    item["chat"] = state["chat_ref"]
                else:
                    item["chat"] = entity_ref(entities.get(item["chat_id"])) or {"id": item["chat_id"]}
                if state["include_media"]:
                    await self._save_media(target, message, item, counters)
                items.append(item)
            if stop_reason is not None:
                if resume_from is not None:  # otherwise the page was not consumed at all
                    next_value = resume_from.id if kind == "chat" else global_cursor(result, resume_from,
                                                                                      use_rate=False)
                break
            if len(result.messages) < page_size:
                finished = True
                next_value = None
                break
            last = result.messages[-1]  # the whole page, also where the sender filter dropped messages
            next_value = last.id if kind == "chat" else global_cursor(result, last)
            if len(items) >= limit:
                stop_reason = "max_messages reached"
                break
            if self._clock() > deadline:
                stop_reason = f"time budget of {self.time_budget:g} s per call used up"
                break
        truncated = stop_reason is not None
        chats: dict[str, Any] = state["chats"]
        for item in items:
            ref = item.get("chat") or {"id": item["chat_id"]}
            chats.setdefault(str(ref["id"]), ref)
        part = state["part"] + 1
        state.update(part=part, next=next_value, finished=finished,
                     total_messages=state["total_messages"] + len(items),
                     media_files=counters["files"], media_skipped=counters["skipped"],
                     media_bytes=counters["bytes"])
        resume_token = target.name if truncated else None
        meta = {
            "part": part,
            "exported_at": iso(self._now()),
            "message_count": len(items),
            "total_messages": state["total_messages"],
            "chats_count": len(chats),
            "truncated": truncated,
            "truncated_reason": stop_reason,
            "resume": resume_token,
        }
        display = self._search_display(state)
        files = {}
        if state["format"] in ("json", "both"):
            path = target / f"part{part}.json"
            path.write_text(json.dumps({"search": display, **meta, "messages": items}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            files["json"] = str(self.host_dir / target.name / path.name)
        if state["format"] in ("markdown", "both"):
            path = target / f"part{part}.md"
            path.write_text(search_markdown(display, meta, items), encoding="utf-8")
            files["markdown"] = str(self.host_dir / target.name / path.name)
        (target / "search.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        result: dict[str, Any] = {
            "directory": str(self.host_dir / target.name),
            "part": part,
            "files": files,
            "message_count": len(items),
            "total_messages": state["total_messages"],
            "chats_count": len(chats),
            "truncated": truncated,
            "truncated_reason": stop_reason,
            "resume": resume_token,
            "media_files": counters["files"],
            "media_skipped": counters["skipped"],
            "first_message_date": items[0]["date"] if items else None,
            "last_message_date": items[-1]["date"] if items else None,
        }
        if state["chat_ref"] is not None:
            result["chat"] = state["chat_ref"]
        return result

