"""Bot service: Telegram handler, workers, liveness supervision and the login command."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sqlite3
import threading
from collections.abc import Awaitable

from telethon import events

from .config import BotSettings, TelegramSettings
from .errors import FatalError
from .processor import VoiceProcessor
from .telegram import TelethonOps, check_connection, connect_authorized, create_client, owner_voice_event
from .transcriber import Transcriber, create_openai_client
from .watchdog import Watchdog
from .work_queue import WorkQueue

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
SHUTDOWN_GRACE = 15


class Bot:
    """Feeds queued voice messages to the processor; duplicates are scheduled once."""

    def __init__(self, processor: VoiceProcessor, queue: WorkQueue):
        self.processor = processor
        self.queue = queue
        self.fatal = asyncio.Event()
        self.fatal_reason = ""
        self._jobs: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
        self._scheduled: set[tuple[int, int]] = set()

    def submit(self, chat_id: int, message_id: int) -> None:
        """Persist a new voice message, then schedule it."""
        self.queue.add(chat_id, message_id)
        self._schedule((chat_id, message_id))

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
                self.fatal_reason = str(exc)
                self.fatal.set()
            except Exception:
                log.exception("message %s: unexpected error, left in the queue for the next start", key)
            finally:
                self._scheduled.discard(key)
                self._jobs.task_done()


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


async def run_bot(settings: BotSettings) -> int:
    try:
        queue = WorkQueue(settings.queue_path)
        client = create_client(settings.telegram, catch_up=True)
    except (OSError, sqlite3.Error) as exc:
        log.error(
            "cannot open files in the data directory %s: %s (it must exist and be writable by the container user)",
            settings.queue_path.parent, exc,
        )
        return EXIT_CONFIG

    transcriber = Transcriber(
        create_openai_client(settings.openai_api_key, settings.openai_organization, settings.openai_timeout),
        settings.transcription_model,
    )
    processor = VoiceProcessor(
        TelethonOps(client),
        transcriber,
        queue,
        max_attempts=settings.max_attempts,
        retry_base_delay=settings.retry_base_delay,
        retry_max_delay=settings.retry_max_delay,
        timeout=settings.processing_timeout,
    )
    bot = Bot(processor, queue)

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
            await connect_authorized(client, settings.telegram)
        except FatalError as exc:
            log.error("%s", exc)
            return EXIT_CONFIG
        except (OSError, asyncio.TimeoutError) as exc:
            log.error("cannot connect to Telegram: %r", exc)
            return EXIT_FAILURE

        me = await client.get_me()
        log.info("logged in as %s (id %s); %d queued message(s) to resume", me.username or me.first_name, me.id,
                 bot.resume_pending())
        workers = [asyncio.create_task(bot.worker()) for _ in range(settings.workers)]
        watchdog = Watchdog(
            lambda: check_connection(client),
            interval=settings.watchdog_interval,
            timeout=settings.watchdog_timeout,
            max_failures=settings.watchdog_max_failures,
        )
        return await supervise(
            disconnected=client.run_until_disconnected(),
            watchdog=watchdog.run(),
            stop=stop.wait(),
            fatal=bot.fatal.wait(),
            reason=lambda: bot.fatal_reason,
        )
    finally:
        for task in workers:
            task.cancel()
        try:
            await asyncio.wait_for(client.disconnect(), 10)
        except Exception as exc:
            log.warning("disconnect failed: %r", exc)
        queue.close()


async def login(settings: TelegramSettings) -> int:
    """Interactive login that creates the session file."""
    client = create_client(settings, receive_updates=False)
    await client.start()
    me = await client.get_me()
    print(f"Logged in as {me.username or me.first_name} (id {me.id}); session saved to {settings.session_path}")
    await client.disconnect()
    return EXIT_OK


def arm_hard_exit(code: int, delay: float = SHUTDOWN_GRACE) -> None:
    """Exit with `code` even if the asyncio shutdown hangs."""

    def hard_exit() -> None:
        os.write(2, f"shutdown did not finish in {delay} s, forcing exit {code}\n".encode())
        os._exit(code)

    timer = threading.Timer(delay, hard_exit)
    timer.daemon = True
    timer.start()
