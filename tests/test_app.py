import asyncio
import logging
import time
from datetime import datetime, timezone

from tests.fakes import FakeTelegram, FakeTranscriber, RecordingSleep
from transcription_bot import app
from transcription_bot.app import EXIT_CONFIG, EXIT_FAILURE, EXIT_OK, SWEEP_MARGIN, SWEEP_MAX_WINDOW, Bot, supervise
from transcription_bot.config import BotSettings
from transcription_bot.errors import FatalError, PermanentError
from transcription_bot.processor import STATUS_TEXT, VoiceProcessor
from transcription_bot.telegram import session_lock
from transcription_bot.work_queue import WorkQueue


def make_bot(path, telegram, transcriber):
    queue = WorkQueue(path)
    processor = VoiceProcessor(telegram, transcriber, queue, max_attempts=3, sleep=RecordingSleep())
    return Bot(processor, queue), queue


async def run_workers(bot, count=2):
    workers = [asyncio.create_task(bot.worker()) for _ in range(count)]
    await asyncio.wait_for(bot.join(), 5)
    for task in workers:
        task.cancel()


async def test_interrupted_message_is_processed_after_restart(tmp_path):
    path = tmp_path / "queue.sqlite3"
    telegram = FakeTelegram()
    telegram.add_voice(1, 10)

    # First run: the process dies while OpenAI is transcribing.
    hanging = FakeTranscriber()
    hanging.block = asyncio.Event()
    bot, queue = make_bot(path, telegram, hanging)
    bot.submit(1, 10)
    worker = asyncio.create_task(bot.worker())
    while hanging.calls == 0:
        await asyncio.sleep(0)
    worker.cancel()
    queue.close()
    assert telegram.caption(1, 10) == STATUS_TEXT

    # Second run: the pending job is resumed from the queue file.
    bot, queue = make_bot(path, telegram, FakeTranscriber("restored text"))
    assert bot.resume_pending() == 1
    await run_workers(bot)
    assert telegram.caption(1, 10) == "restored text"
    assert queue.pending() == []


async def test_duplicate_updates_are_processed_once(tmp_path):
    telegram = FakeTelegram()
    telegram.add_voice(1, 10)
    telegram.add_voice(1, 11)
    transcriber = FakeTranscriber()
    bot, queue = make_bot(tmp_path / "q.sqlite3", telegram, transcriber)
    bot.submit(1, 10)
    bot.submit(1, 10)  # catch-up redelivery while still queued
    bot.submit(1, 11)
    await run_workers(bot)
    assert transcriber.calls == 2
    bot.submit(1, 10)  # redelivery after it was transcribed
    await run_workers(bot)
    assert transcriber.calls == 2
    assert queue.pending() == []


async def test_fatal_error_in_worker_is_reported(tmp_path):
    telegram = FakeTelegram()
    telegram.errors["fetch"] = [FatalError("session revoked")]
    bot, queue = make_bot(tmp_path / "q.sqlite3", telegram, FakeTranscriber())
    bot.submit(1, 10)
    await run_workers(bot)
    assert bot.fatal.is_set() and "revoked" in bot.fatal_reason
    assert len(queue.pending()) == 1


async def test_resubmission_while_the_job_is_being_removed_is_not_lost(tmp_path):
    # A resumed job fails (chat not yet in the entity cache) while catch-up delivers the same message.
    telegram = FakeTelegram()
    telegram.add_voice(1, 10)
    gate = asyncio.Event()
    fetch = telegram.fetch
    calls = 0

    async def flaky_fetch(chat_id, message_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            await gate.wait()
        if calls <= 2:
            raise PermanentError("Telegram chat not found")
        return await fetch(chat_id, message_id)

    telegram.fetch = flaky_fetch
    bot, queue = make_bot(tmp_path / "q.sqlite3", telegram, FakeTranscriber())
    bot.submit(1, 10)
    workers = [asyncio.create_task(bot.worker())]
    while calls == 0:
        await asyncio.sleep(0)
    bot.submit(1, 10)  # the catch-up replay
    gate.set()
    await asyncio.wait_for(bot.join(), 5)
    workers[0].cancel()
    assert telegram.caption(1, 10) == "hello world"
    assert queue.pending() == []


async def test_queue_file_errors_stop_the_bot(tmp_path):
    telegram = FakeTelegram()
    telegram.add_voice(1, 10)
    bot, queue = make_bot(tmp_path / "q.sqlite3", telegram, FakeTranscriber())
    queue.close()  # every further SQLite call fails
    bot.submit(1, 10)  # in the update handler
    assert bot.fatal.is_set() and "queue file" in bot.fatal_reason

    bot, queue = make_bot(tmp_path / "q2.sqlite3", telegram, FakeTranscriber())
    bot.submit(1, 10)
    queue.close()
    await run_workers(bot)  # in a worker
    assert bot.fatal.is_set() and "queue file" in bot.fatal_reason

    bot.fatal.clear()
    bot.beat(time.time())  # heartbeat
    assert bot.fatal.is_set()


async def test_sweep_submits_untranscribed_messages_since_the_last_heartbeat(tmp_path):
    telegram = FakeTelegram()
    telegram.add_voice(1, 10)
    telegram.add_voice(1, 11, caption="transcribed before")
    transcriber = FakeTranscriber()
    bot, queue = make_bot(tmp_path / "q.sqlite3", telegram, transcriber)
    now = time.time()
    queue.set_heartbeat(now - 3600)
    queue.add(1, 10)  # also queued already: processed once
    asked = []

    async def find(since):
        asked.append(since)
        return [(1, 10), (1, 11)]

    assert await bot.sweep(find, now)
    assert asked == [datetime.fromtimestamp(now - 3600 - SWEEP_MARGIN, timezone.utc)]
    bot.resume_pending()  # (1, 10) is already scheduled
    await run_workers(bot)
    assert telegram.caption(1, 10) == "hello world" and telegram.caption(1, 11) == "transcribed before"
    assert transcriber.calls == 1 and queue.pending() == []

    queue.set_heartbeat(now - 10 * 24 * 3600)  # down for ten days: at most 48 h back
    assert await bot.sweep(find, now)
    assert asked[-1] == datetime.fromtimestamp(now - SWEEP_MAX_WINDOW, timezone.utc)


async def test_sweep_is_skipped_on_the_first_start_and_never_fatal(tmp_path, caplog):
    bot, queue = make_bot(tmp_path / "q.sqlite3", FakeTelegram(), FakeTranscriber())
    calls = 0

    async def broken(since):
        nonlocal calls
        calls += 1
        raise ConnectionError("Telegram is down")

    assert await bot.sweep(broken, time.time())  # no heartbeat yet: nothing to look for
    assert calls == 0
    queue.set_heartbeat(time.time() - 60)
    assert await bot.sweep(broken, time.time()) is False
    assert calls == 1 and not bot.fatal.is_set()
    assert "startup sweep failed" in caplog.text


def bot_settings(tmp_path):
    return BotSettings.from_env({"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "hash", "OPENAI_API_KEY": "key",
                                 "DATA_DIR": str(tmp_path / "data")})


async def test_run_without_session_explains_how_to_log_in(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    assert await app.run_bot(bot_settings(tmp_path)) == EXIT_CONFIG
    assert "is not authorized. Log in interactively with `docker compose run --rm bot transcription-bot login`" \
        in caplog.text
    assert (tmp_path / "data" / "queue.sqlite3").exists()  # the data directory is writable


async def test_second_instance_is_refused(tmp_path, caplog):
    settings = bot_settings(tmp_path)
    with session_lock(settings.telegram):
        assert await app.run_bot(settings) == EXIT_CONFIG
        assert await app.login(settings.telegram) == EXIT_CONFIG
    assert caplog.text.count("another instance is running") == 2


async def test_login_with_unusable_data_directory_fails_cleanly(tmp_path, caplog):
    (tmp_path / "data").write_text("not a directory")
    settings = bot_settings(tmp_path)
    assert await app.login(settings.telegram) == EXIT_CONFIG
    assert await app.run_bot(settings) == EXIT_CONFIG
    assert caplog.text.count("cannot open files in the data directory") == 2


async def never():
    await asyncio.Event().wait()


async def done():
    return None


async def disconnected_with_error():
    raise ConnectionError("gave up reconnecting")


async def test_supervise_exit_codes():
    assert await supervise(disconnected=never(), watchdog=never(), stop=done(), fatal=never()) == EXIT_OK
    assert await supervise(disconnected=never(), watchdog=done(), stop=never(), fatal=never()) == EXIT_FAILURE
    assert await supervise(disconnected=done(), watchdog=never(), stop=never(), fatal=never()) == EXIT_FAILURE
    assert await supervise(
        disconnected=disconnected_with_error(), watchdog=never(), stop=never(), fatal=never()
    ) == EXIT_FAILURE
    assert await supervise(disconnected=never(), watchdog=never(), stop=never(), fatal=done()) == EXIT_FAILURE
