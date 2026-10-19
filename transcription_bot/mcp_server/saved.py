"""The only writes the MCP server can make: into the owner's own Saved Messages (opt-in).

Enabled with `MCP_SAVED_MESSAGES_WRITE=1`. Every request goes through the guard, which accepts
`messages.sendMessage` / `messages.forwardMessages` only with `InputPeerSelf` as the target and
no options beyond plain delivery. Results carry message ids only, never the content.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

from telethon import errors, functions, types, utils

from .reader import MAX_LIMIT, ReaderError, TelegramReader

# Telegram's limit for one text message (counted in UTF-16 code units, which is never less than
# the number of characters).
MAX_CHUNK = 4096
MAX_TEXT_PER_CALL = 16_000
MAX_FORWARD_IDS = MAX_LIMIT
# A confused model must not flood Saved Messages: at most this many write calls per window.
RATE_LIMIT_CALLS = 20
RATE_LIMIT_WINDOW = 60.0


def _utf16_len(text: str) -> int:
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def _prefix_within(text: str, limit: int) -> int:
    """Length (in characters) of the longest prefix of `text` that fits into `limit` UTF-16 units."""
    units = 0
    for index, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return index
    return len(text)


def split_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split plain text into messages of at most `limit` UTF-16 units, preferring line and word breaks."""
    chunks = []
    rest = text
    while rest:
        if _utf16_len(rest) <= limit:
            chunks.append(rest)
            break
        cut = _prefix_within(rest, limit)
        window = rest[:cut]
        pos = window.rfind("\n")
        if pos < cut // 2:
            pos = max(window.rfind(" "), window.rfind("\t"), pos)
        pos = pos + 1 if pos > 0 else cut
        chunks.append(rest[:pos])
        rest = rest[pos:]
    # Telegram trims every message and refuses empty ones.
    return [chunk for chunk in chunks if chunk.strip()]


def new_message_ids(request, result) -> list[int]:
    """Ids of the messages a send/forward request created, in request order."""
    if isinstance(result, types.UpdateShortSentMessage):
        return [result.id]
    if isinstance(result, types.UpdateShort):
        updates = [result.update]
    else:
        updates = list(getattr(result, "updates", None) or [])
    by_random = {u.random_id: u.id for u in updates if isinstance(u, types.UpdateMessageID)}
    randoms = request.random_id if isinstance(request.random_id, list) else [request.random_id]
    ids = [by_random[r] for r in randoms if r in by_random]
    if ids:
        return ids
    return [u.message.id for u in updates
            if isinstance(u, types.UpdateNewMessage) and isinstance(u.message, types.Message)]


class SavedMessagesWriter:
    def __init__(
        self,
        reader: TelegramReader,
        *,
        max_calls: int = RATE_LIMIT_CALLS,
        window: float = RATE_LIMIT_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.reader = reader
        self.client = reader.client
        self.max_calls = max_calls
        self.window = window
        self._clock = clock
        self._calls: deque[float] = deque()

    def _check_rate(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] >= self.window:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            retry = max(1, round(self.window - (now - self._calls[0])))
            raise ReaderError(f"rate limit: at most {self.max_calls} Saved Messages writes per "
                              f"{self.window:g} s; retry in {retry} s")

    def _take_slot(self) -> None:
        self._check_rate()
        self._calls.append(self._clock())

    async def send_to_saved(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ReaderError("text must not be empty")
        if len(text) > MAX_TEXT_PER_CALL:
            raise ReaderError(f"text is too long: at most {MAX_TEXT_PER_CALL} characters per call")
        chunks = split_text(text)
        self._take_slot()
        sent: list[int] = []
        for chunk in chunks:
            request = functions.messages.SendMessageRequest(
                peer=types.InputPeerSelf(), message=chunk, no_webpage=True)
            try:
                result = await self.client(request)
            except errors.RPCError as exc:
                if not sent:
                    raise
                raise ReaderError(f"sent {len(sent)} of {len(chunks)} parts (message_ids {sent}), "
                                  f"then Telegram error: {exc.message}") from exc
            sent.extend(new_message_ids(request, result))
        return {"message_ids": sent}

    async def forward_to_saved(self, chat: str | int, message_ids: list[int]) -> dict[str, Any]:
        ids = list(dict.fromkeys(message_ids or []))
        if not ids:
            raise ReaderError("message_ids must not be empty")
        if len(ids) > MAX_FORWARD_IDS:
            raise ReaderError(f"at most {MAX_FORWARD_IDS} message ids per call")
        if any(type(i) is not int or i < 1 for i in ids):
            raise ReaderError("message ids must be positive integers")
        self._check_rate()
        entity = await self.reader.resolve(chat)
        if getattr(entity, "noforwards", False):
            raise ReaderError("this chat protects its content: its messages cannot be forwarded")
        found = await self.client.get_messages(entity, ids=ids)
        present = {m.id: m for m in found if m is not None and not isinstance(m, types.MessageEmpty)}
        missing = [i for i in ids if i not in present]
        if missing:
            raise ReaderError(f"messages not found in this chat: {missing}")
        service = [i for i in ids if isinstance(present[i], types.MessageService)]
        if service:
            raise ReaderError(f"service messages cannot be forwarded: {service}")
        protected = [i for i in ids if getattr(present[i], "noforwards", False)]
        if protected:
            raise ReaderError(f"protected content cannot be forwarded: {protected}")
        self._take_slot()
        request = functions.messages.ForwardMessagesRequest(
            from_peer=utils.get_input_peer(entity), id=ids, to_peer=types.InputPeerSelf())
        try:
            result = await self.client(request)
        except errors.ChatForwardsRestrictedError as exc:
            raise ReaderError("this chat protects its content: its messages cannot be forwarded") from exc
        except errors.MessageIdInvalidError as exc:
            raise ReaderError("Telegram did not find some of these messages (message id invalid)") from exc
        return {"message_ids": new_message_ids(request, result)}
