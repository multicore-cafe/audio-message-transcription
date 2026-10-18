"""Bot service: Telegram handler, workers, liveness supervision and the login command."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from telethon import errors, events

from .config import BotSettings, TelegramSettings
from .errors import FatalError
from .processor import VoiceProcessor
from .telegram import (
    STARTUP_TIMEOUT,
    TelethonOps,
    check_connection,
    connect_authorized,
    create_client,
    find_untranscribed,
    login_hint,
    owner_voice_event,
    session_lock,
)
from .transcriber import Transcriber, create_openai_client
from .watchdog import Watchdog
from .work_queue import WorkQueue

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
SHUTDOWN_GRACE = 15
WORKERS = 2
# The startup sweep looks for voice messages sent while the bot was down: from the last heartbeat
# (minus a margin for updates that were still on their way) but at most this far back.
SWEEP_MAX_WINDOW = 48 * 3600
SWEEP_MARGIN = 15 * 60
SWEEP_TIMEOUT = 120


class Bot:
    """Feeds queued voice messages to the processor; duplicates are scheduled once."""

    def __init__(self, processor: VoiceProcessor, queue: WorkQueue):
        self.processor = processor
        self.queue = queue
        self.fatal = asyncio.Event()
        self.fatal_reason = ""
        self._jobs: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
        self._scheduled: set[tuple[int, int]] = set()
        self._resubmitted: set[tuple[int, int]] = set()

    def fail(self, reason: str) -> None:
        self.fatal_reason = reason
        self.fatal.set()

    def submit(self, chat_id: int, message_id: int) -> None:
        """Persist a new voice message, then schedule it. A broken queue file stops the bot."""
        key = (chat_id, message_id)
        try:
            self.queue.add(chat_id, message_id)
        except sqlite3.Error as exc:
            self.fail(f"cannot write the queue file: {exc!r}")
            return
        if key in self._scheduled:
            # The running job may be about to delete the row; submit again once it is finished.
            self._resubmitted.add(key)
        else:
            self._schedule(key)

    def resume_pending(self) -> int:
        jobs = self.queue.pending()
        for job in jobs:
            self._schedule((job.chat_id, job.message_id))
        return len(jobs)

    def _schedule(self, key: tuple[int, int]) -> None:
        if key not in self._scheduled:
            self._scheduled.add(key)
            self._jobs.put_nowait(key)

    async def join(self) -> None:
        await self._jobs.join()

    async def worker(self) -> None:
        while True:
            key = await self._jobs.get()
            try:
                await self.processor.process(*key)
            except FatalError as exc:
                self.fail(str(exc))
            except sqlite3.Error as exc:
                self.fail(f"queue file error: {exc!r}")
            except Exception:
                log.exception("message %s: unexpected error, left in the queue for the next start", key)
            finally:
                self._scheduled.discard(key)
                if key in self._resubmitted:
                    self._resubmitted.discard(key)
                    if not self.fatal.is_set():
                        self.submit(*key)
                self._jobs.task_done()

    async def sweep(self, find: Callable[[datetime], Awaitable[list[tuple[int, int]]]], now: float) -> bool:
        """Submit voice messages sent since the last heartbeat that are still untranscribed.

        Best effort: returns False (and logs) if the search failed. Catch-up of missed updates
        does the main work; this covers chats Telethon does not catch up (see README).
        """
        last = self.queue.heartbeat()
        if last is None:
            log.info("startup sweep skipped: no heartbeat yet (first start)")
            return True
        since = max(last - SWEEP_MARGIN, now - SWEEP_MAX_WINDOW)
        try:
            found = await asyncio.wait_for(find(datetime.fromtimestamp(since, timezone.utc)), SWEEP_TIMEOUT)
        except Exception as exc:
            log.warning("startup sweep failed (the next start tries again): %r", exc)
            return False
        for chat_id, message_id in found:
            self.submit(chat_id, message_id)
        log.info("startup sweep: %d untranscribed voice message(s) since %s",
                 len(found), datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="seconds"))
        return True

    def beat(self, when: float) -> None:
        try:
            self.queue.set_heartbeat(when)
        except sqlite3.Error as exc:
            self.fail(f"cannot write the queue file: {exc!r}")


async def supervise(
    *, disconnected: Awaitable, watchdog: Awaitable, stop: Awaitable, fatal: Awaitable, reason=lambda: ""
) -> int:
    """Wait until something ends the service and return the exit code."""
    tasks = {
        name: asyncio.ensure_future(aw)
        for name, aw in (("stop", stop), ("fatal", fatal), ("watchdog", watchdog), ("disconnected", disconnected))
    }
    try:
        await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks.values():
            task.cancel()
    if tasks["stop"].done() and not tasks["stop"].cancelled():
        log.info("stop requested")
        return EXIT_OK
    if tasks["fatal"].done() and not tasks["fatal"].cancelled():
        log.error("fatal error: %s", reason())
    elif tasks["watchdog"].done() and not tasks["watchdog"].cancelled():
        log.error("exiting: watchdog detected a dead Telegram connection")
    else:
        task = tasks["disconnected"]
        error = None if task.cancelled() else task.exception()
        log.error("exiting: Telegram connection lost for good (%r)", error)
    return EXIT_FAILURE


def report_file_error(exc: Exception, directory: Path) -> int:
    """Log why the lock, session or queue file could not be opened; returns the exit code."""
    if isinstance(exc, FatalError):
        log.error("%s", exc)
    else:
        log.error("cannot open files in the data directory %s: %s (it must be writable by the container user)",
                  directory, exc)
    return EXIT_CONFIG


async def run_bot(settings: BotSettings) -> int:
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(session_lock(settings.telegram))
            queue = WorkQueue(settings.queue_path)
            stack.callback(queue.close)
            client = create_client(settings.telegram, catch_up=True)
        except (FatalError, OSError, sqlite3.Error) as exc:
            return report_file_error(exc, settings.queue_path.parent)
        return await _serve(settings, client, queue)


async def _serve(settings: BotSettings, client, queue: WorkQueue) -> int:
    transcriber = Transcriber(
        create_openai_client(settings.openai_api_key, settings.openai_organization), settings.transcription_model
    )
    bot = Bot(VoiceProcessor(TelethonOps(client), transcriber, queue), queue)

    async def on_voice(event: events.NewMessage.Event) -> None:
        bot.submit(event.chat_id, event.message.id)

    client.add_event_handler(on_voice, owner_voice_event())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    workers: list[asyncio.Task] = []
    try:
        try:
            await asyncio.wait_for(connect_authorized(client, login_hint(settings.telegram)), STARTUP_TIMEOUT)
            me = await asyncio.wait_for(client.get_me(), STARTUP_TIMEOUT)
        except FatalError as exc:
            log.error("%s", exc)
            return EXIT_CONFIG
        except (OSError, TimeoutError, errors.RPCError) as exc:
            log.error("cannot connect to Telegram: %r", exc)
            return EXIT_FAILURE

        started = time.time()
        resumed = bot.resume_pending()
        log.info("logged in as %s (id %s); %d queued message(s) to resume", me.username or me.first_name, me.id,
                 resumed)
        workers = [asyncio.create_task(bot.worker()) for _ in range(WORKERS)]
        swept = await bot.sweep(lambda since: find_untranscribed(client, since), started)
        if swept:
            bot.beat(started)

        async def check() -> None:
            await check_connection(client)
            if swept:
                bot.beat(time.time())

        return await supervise(
            disconnected=client.run_until_disconnected(),
            watchdog=Watchdog(check).run(),
            stop=stop.wait(),
            fatal=bot.fatal.wait(),
            reason=lambda: bot.fatal_reason,
        )
    finally:
        for task in workers:
            task.cancel()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        try:
            await asyncio.wait_for(client.disconnect(), 10)
        except Exception as exc:
            log.warning("disconnect failed: %r", exc)


async def interactive_login(settings: TelegramSettings, open_client: Callable[[], object]) -> int:
    """Interactive login that creates a session file; `open_client` builds the client."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(session_lock(settings))
            client = open_client()
        except (FatalError, OSError, sqlite3.Error) as exc:
            return report_file_error(exc, settings.session_path.parent)
        await client.start()
        me = await client.get_me()
        print(f"Logged in as {me.username or me.first_name} (id {me.id}); session saved to {settings.session_path}")
        await client.disconnect()
        return EXIT_OK


async def login(settings: TelegramSettings) -> int:
    return await interactive_login(settings, lambda: create_client(settings, receive_updates=False))


def arm_hard_exit(code: int, delay: float = SHUTDOWN_GRACE) -> None:
    """Exit with `code` even if the asyncio shutdown hangs."""

    def hard_exit() -> None:
        os.write(2, f"shutdown did not finish in {delay} s, forcing exit {code}\n".encode())
        os._exit(code)

    timer = threading.Timer(delay, hard_exit)
    timer.daemon = True
    timer.start()
