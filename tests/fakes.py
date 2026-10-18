"""In-memory stand-ins for Telegram and OpenAI."""

from __future__ import annotations

import asyncio

from transcription_bot.errors import CaptionTooLong
from transcription_bot.processor import VoiceMessage


class FakeTelegram:
    def __init__(self, caption_limit: int = 1024):
        self.messages: dict[tuple[int, int], VoiceMessage] = {}
        self.caption_limit = caption_limit
        self.edits: list[tuple[tuple[int, int], str, bool]] = []
        self.replies: list[tuple[tuple[int, int], str]] = []
        self.downloads = 0
        # Exceptions raised by the next calls of an operation, in order; `None` lets that call succeed.
        self.errors: dict[str, list[BaseException | None]] = {}

    def add_voice(self, chat_id: int, message_id: int, caption: str = "", size: int = 1000) -> None:
        self.messages[(chat_id, message_id)] = VoiceMessage(chat_id, message_id, caption, size)

    def caption(self, chat_id: int, message_id: int) -> str:
        return self.messages[(chat_id, message_id)].caption

    def _maybe_fail(self, op: str) -> None:
        pending = self.errors.get(op)
        if pending:
            error = pending.pop(0)
            if error is not None:
                raise error

    async def fetch(self, chat_id, message_id):
        self._maybe_fail("fetch")
        message = self.messages.get((chat_id, message_id))
        return None if message is None else VoiceMessage(**vars(message))

    async def download(self, message):
        self._maybe_fail("download")
        self.downloads += 1
        return b"OggS" + b"\0" * 100

    async def set_caption(self, chat_id, message_id, text, *, italic=False):
        self._maybe_fail("set_caption")
        if len(text) > self.caption_limit:
            raise CaptionTooLong()
        self.edits.append(((chat_id, message_id), text, italic))
        self.messages[(chat_id, message_id)].caption = text

    async def reply(self, chat_id, message_id, text):
        self._maybe_fail("reply")
        assert len(text) <= 4096
        self.replies.append(((chat_id, message_id), text))


class FakeTranscriber:
    def __init__(self, text: str = "hello world"):
        self.text = text
        self.calls = 0
        self.errors: list[BaseException] = []
        self.block: asyncio.Event | None = None

    async def transcribe(self, audio: bytes) -> str:
        self.calls += 1
        if self.block is not None:
            await self.block.wait()
        if self.errors:
            raise self.errors.pop(0)
        return self.text


class RecordingSleep:
    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
