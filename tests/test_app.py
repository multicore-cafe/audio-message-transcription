import asyncio

from tests.fakes import FakeTelegram, FakeTranscriber, RecordingSleep
from transcription_bot.app import EXIT_FAILURE, EXIT_OK, Bot, supervise
from transcription_bot.errors import FatalError
from transcription_bot.processor import STATUS_TEXT, VoiceProcessor
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
