"""Transcription of one queued voice message: idempotent, retried, never fatal for the bot."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import CaptionTooLong, FatalError, FloodWait, PermanentError, TransientError
from .transcriber import MAX_FILE_SIZE, TOO_LARGE_NOTE
from .work_queue import Job, WorkQueue

log = logging.getLogger(__name__)

STATUS_TEXT = "Transcribing..."
STATUS_MARKERS = frozenset({STATUS_TEXT})
SEE_BELOW_TEXT = "See transcription below"
NO_SPEECH_TEXT = "(no speech recognized)"
FAILURE_PREFIX = "Transcription failed: "
MAX_MESSAGE_LENGTH = 4096

# Attempts that end in a crash, a restart in the middle of the work or an unexpected exception.
MAX_ATTEMPTS = 5
# Outages (network, Telegram, OpenAI, OpenAI account problems) are retried this long after the
# message was queued. Telegram allows editing for 48 h, so the failure note still fits.
RETRY_WINDOW = 24 * 3600
RETRY_BASE_DELAY = 10
RETRY_MAX_DELAY = 300
MAX_FLOOD_SLEEP = 3600
PROCESSING_TIMEOUT = 900

# Errors that say nothing about the message itself.
OUTAGE_ERRORS = (TransientError, TimeoutError, OSError)


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


def _duration(seconds: float) -> str:
    return f"{seconds / 3600:g} h" if seconds >= 3600 else f"{seconds:g} s"


class VoiceProcessor:
    def __init__(
        self,
        telegram: TelegramOps,
        transcriber: Transcribe,
        queue: WorkQueue,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        retry_window: float = RETRY_WINDOW,
        retry_base_delay: float = RETRY_BASE_DELAY,
        retry_max_delay: float = RETRY_MAX_DELAY,
        timeout: float = PROCESSING_TIMEOUT,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.telegram = telegram
        self.transcriber = transcriber
        self.queue = queue
        self.max_attempts = max_attempts
        self.retry_window = retry_window
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock

    def backoff(self, failures: int) -> float:
        return min(self.retry_max_delay, self.retry_base_delay * 2 ** (failures - 1))

    async def process(self, chat_id: int, message_id: int) -> str:
        """Process a queued job until it is done or given up; returns the outcome name.

        SQLite errors and `FatalError` propagate; the job then stays queued for the next start.
        """
        key = (chat_id, message_id)
        failures = 0
        while True:
            job = self.queue.start_attempt(chat_id, message_id)
            if job.attempts > self.max_attempts:
                log.error("message %s: attempt %d exceeds the limit", key, job.attempts)
                self.queue.end_attempt(chat_id, message_id)
                return await self._give_up(job, f"gave up after {self.max_attempts} attempts")
            try:
                outcome = await asyncio.wait_for(self._run(job), self.timeout)
            except asyncio.CancelledError:
                self.queue.end_attempt(chat_id, message_id)  # shutdown, not a crash
                raise
            except FatalError:
                self.queue.end_attempt(chat_id, message_id)
                raise
            except sqlite3.Error:
                raise  # the queue itself is broken: the bot stops (the job stays for the next start)
            except PermanentError as exc:
                log.error("message %s: %s", key, exc.note)
                self.queue.end_attempt(chat_id, message_id)
                return await self._give_up(job, exc.note)
            except OUTAGE_ERRORS as exc:
                self.queue.end_attempt(chat_id, message_id)
                if self._clock() - job.added_at > self.retry_window:
                    window = _duration(self.retry_window)
                    log.error("message %s: still failing after %s: %r", key, window, exc)
                    return await self._give_up(job, f"gave up after {window} of errors ({str(exc) or type(exc).__name__})")
                failures += 1
                if isinstance(exc, FloodWait):
                    delay = min(exc.seconds + 1, MAX_FLOOD_SLEEP)
                else:
                    delay = self.backoff(failures)
                log.warning("message %s: %r; retrying in %s s", key, exc, delay)
                await self._sleep(delay)
            except Exception as exc:
                # A bug or an unknown error: the attempt stays counted, like a crash.
                log.warning("message %s: attempt %d/%d failed: %r", key, job.attempts, self.max_attempts, exc,
                            exc_info=True)
                await self._sleep(self.backoff(job.attempts))
            else:
                self.queue.remove(chat_id, message_id)
                log.info("message %s: %s", key, outcome)
                return outcome

    async def _run(self, job: Job) -> str:
        chat_id, message_id = job.chat_id, job.message_id
        message = await self.telegram.fetch(chat_id, message_id)
        if message is None:
            return "gone"
        if not message.is_voice:
            return "skipped"
        if message.caption and message.caption not in STATUS_MARKERS:
            return "skipped"  # already transcribed or captioned by the user
        if message.size is not None and message.size > MAX_FILE_SIZE:
            raise PermanentError(TOO_LARGE_NOTE)
        text = job.text
        if text is None:
            if message.caption != STATUS_TEXT:
                await self.telegram.set_caption(chat_id, message_id, STATUS_TEXT, italic=True)
            audio = await self.telegram.download(message)
            text = await self.transcriber.transcribe(audio)
            # Kept, so a retry or a restart does not ask OpenAI again.
            self.queue.save_text(chat_id, message_id, text)
        if not text:
            await self.telegram.set_caption(chat_id, message_id, NO_SPEECH_TEXT, italic=True)
            return "done"
        try:
            await self.telegram.set_caption(chat_id, message_id, text)
        except CaptionTooLong:
            for index, chunk in enumerate(split_text(text)):
                if index < job.sent_chunks:
                    continue
                await self.telegram.reply(chat_id, message_id, chunk)
                self.queue.save_sent_chunks(chat_id, message_id, index + 1)
            await self.telegram.set_caption(chat_id, message_id, SEE_BELOW_TEXT, italic=True)
        return "done"

    async def _give_up(self, job: Job, note: str) -> str:
        """Write the failure note, then drop the job. Until the note is written the job stays queued."""
        chat_id, message_id = job.chat_id, job.message_id
        failures = 0
        while True:
            try:
                await self._write_note(chat_id, message_id, note)
                break
            except FatalError:
                raise
            except PermanentError as exc:
                # Telegram refuses the edit for good (deleted, edit window over); nothing left to do.
                log.error("message %s: cannot write the failure note: %s", (chat_id, message_id), exc.note)
                break
            except Exception as exc:
                failures += 1
                delay = min(exc.seconds + 1, MAX_FLOOD_SLEEP) if isinstance(exc, FloodWait) else self.backoff(failures)
                log.warning("message %s: could not write the failure note: %r; retrying in %s s",
                            (chat_id, message_id), exc, delay)
                await self._sleep(delay)
        self.queue.remove(chat_id, message_id)
        log.info("message %s: failed", (chat_id, message_id))
        return "failed"

    async def _write_note(self, chat_id: int, message_id: int, note: str) -> None:
        message = await self.telegram.fetch(chat_id, message_id)
        if message is None or (message.caption and message.caption not in STATUS_MARKERS):
            return  # deleted, or it got a real caption meanwhile
        await self.telegram.set_caption(chat_id, message_id, FAILURE_PREFIX + note, italic=True)
