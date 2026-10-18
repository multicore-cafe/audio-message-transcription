"""Transcription of one queued voice message: idempotent, retried, never fatal for the bot."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import CaptionTooLong, FatalError, FloodWait, PermanentError, TransientError
from .transcriber import MAX_FILE_SIZE, TOO_LARGE_NOTE
from .work_queue import WorkQueue

log = logging.getLogger(__name__)

STATUS_TEXT = "Transcribing..."
# Status captions of the old bot; a message left with one of them is retried too.
LEGACY_STATUS_TEXTS = ("Downloading and converting...", "Text recognizing...")
STATUS_MARKERS = frozenset({STATUS_TEXT, *LEGACY_STATUS_TEXTS})
SEE_BELOW_TEXT = "See transcription below"
NO_SPEECH_TEXT = "(no speech recognized)"
FAILURE_PREFIX = "Transcription failed: "
MAX_MESSAGE_LENGTH = 4096


@dataclass
class VoiceMessage:
    chat_id: int
    message_id: int
    caption: str
    size: int | None
    is_voice: bool = True
    raw: Any = None


class TelegramOps(Protocol):
    async def fetch(self, chat_id: int, message_id: int) -> VoiceMessage | None: ...
    async def download(self, message: VoiceMessage) -> bytes: ...
    async def set_caption(self, chat_id: int, message_id: int, text: str, *, italic: bool = False) -> None: ...
    async def reply(self, chat_id: int, message_id: int, text: str) -> None: ...


class Transcribe(Protocol):
    async def transcribe(self, audio: bytes) -> str: ...


def split_text(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split into chunks of at most `limit` characters, preferring line and word boundaries."""
    chunks = []
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


class VoiceProcessor:
    def __init__(
        self,
        telegram: TelegramOps,
        transcriber: Transcribe,
        queue: WorkQueue,
        *,
        max_attempts: int = 5,
        retry_base_delay: float = 10,
        retry_max_delay: float = 300,
        timeout: float = 900,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.telegram = telegram
        self.transcriber = transcriber
        self.queue = queue
        self.max_attempts = max_attempts
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self.timeout = timeout
        self._sleep = sleep
        # Reply chunks already sent per message, so a retry in the same run does not repeat them.
        self._sent_chunks: dict[tuple[int, int], int] = {}

    def backoff(self, attempt: int) -> float:
        return min(self.retry_max_delay, self.retry_base_delay * 2 ** (attempt - 1))

    async def process(self, chat_id: int, message_id: int) -> str:
        """Process a queued job until it is done or dropped; returns the outcome name."""
        key = (chat_id, message_id)
        attempts = self.queue.start_attempt(chat_id, message_id)
        try:
            while True:
                if attempts > self.max_attempts:
                    log.error("message %s: dropped, attempt %d exceeds the limit", key, attempts)
                    await self._fail(chat_id, message_id, f"gave up after {self.max_attempts} attempts")
                    outcome = "failed"
                    break
                try:
                    outcome = await asyncio.wait_for(self._run(chat_id, message_id), self.timeout)
                    break
                except FloodWait as exc:
                    log.warning("message %s: Telegram flood wait, sleeping %s s", key, exc.seconds)
                    await self._sleep(exc.seconds + 1)
                except PermanentError as exc:
                    log.error("message %s: %s", key, exc.note)
                    await self._fail(chat_id, message_id, exc.note)
                    outcome = "failed"
                    break
                except FatalError:
                    raise
                except Exception as exc:
                    log.warning(
                        "message %s: attempt %d/%d failed: %r",
                        key, attempts, self.max_attempts, exc,
                        exc_info=not isinstance(exc, (TransientError, TimeoutError, OSError)),  # traceback for bugs
                    )
                    if attempts >= self.max_attempts:
                        await self._fail(chat_id, message_id, f"gave up after {self.max_attempts} attempts")
                        outcome = "failed"
                        break
                    await self._sleep(self.backoff(attempts))
                    attempts = self.queue.start_attempt(chat_id, message_id)
        finally:
            self._sent_chunks.pop(key, None)
        self.queue.remove(chat_id, message_id)
        log.info("message %s: %s", key, outcome)
        return outcome

    async def _run(self, chat_id: int, message_id: int) -> str:
        message = await self.telegram.fetch(chat_id, message_id)
        if message is None:
            return "gone"
        if not message.is_voice:
            return "skipped"
        if message.caption and message.caption not in STATUS_MARKERS:
            return "skipped"  # already transcribed or captioned by the user
        if message.size is not None and message.size > MAX_FILE_SIZE:
            raise PermanentError(TOO_LARGE_NOTE)
        if message.caption != STATUS_TEXT:
            await self.telegram.set_caption(chat_id, message_id, STATUS_TEXT, italic=True)
        audio = await self.telegram.download(message)
        text = await self.transcriber.transcribe(audio)
        if not text:
            await self.telegram.set_caption(chat_id, message_id, NO_SPEECH_TEXT, italic=True)
            return "done"
        try:
            await self.telegram.set_caption(chat_id, message_id, text)
        except CaptionTooLong:
            key = (chat_id, message_id)
            for index, chunk in enumerate(split_text(text)):
                if index < self._sent_chunks.get(key, 0):
                    continue
                await self.telegram.reply(chat_id, message_id, chunk)
                self._sent_chunks[key] = index + 1
            await self.telegram.set_caption(chat_id, message_id, SEE_BELOW_TEXT, italic=True)
        return "done"

    async def _fail(self, chat_id: int, message_id: int, note: str) -> None:
        """Best effort: put the failure note unless the message is gone or has a real caption."""
        try:
            message = await self.telegram.fetch(chat_id, message_id)
            if message is None or (message.caption and message.caption not in STATUS_MARKERS):
                return
            await self.telegram.set_caption(chat_id, message_id, FAILURE_PREFIX + note, italic=True)
        except FatalError:
            raise
        except Exception as exc:
            log.warning("message %s: could not write the failure note: %r", (chat_id, message_id), exc)
