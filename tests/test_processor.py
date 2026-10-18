import asyncio
import time

import pytest

from transcription_bot.errors import FatalError, FloodWait, PermanentError, TransientError
from transcription_bot.processor import (
    FAILURE_PREFIX,
    NO_SPEECH_TEXT,
    SEE_BELOW_TEXT,
    STATUS_TEXT,
    VoiceProcessor,
    split_text,
)
from tests.fakes import FakeTranscriber
from transcription_bot.transcriber import MAX_FILE_SIZE


async def test_transcribes_into_caption(processor, telegram, queue):
    telegram.add_voice(1, 10)
    queue.add(1, 10)
    assert await processor.process(1, 10) == "done"
    assert [text for _, text, _ in telegram.edits] == [STATUS_TEXT, "hello world"]
    assert telegram.edits[0][2] is True  # status is italic
    assert telegram.edits[1][2] is False
    assert queue.pending() == []


async def test_skips_already_transcribed_message(processor, telegram, transcriber, queue):
    telegram.add_voice(1, 10, caption="earlier transcription")
    queue.add(1, 10)
    assert await processor.process(1, 10) == "skipped"
    assert telegram.edits == [] and transcriber.calls == 0
    assert queue.pending() == []


async def test_retries_message_left_with_status_caption(processor, telegram):
    telegram.add_voice(1, 10, caption=STATUS_TEXT)
    assert await processor.process(1, 10) == "done"
    assert telegram.caption(1, 10) == "hello world"
    assert [text for _, text, _ in telegram.edits] == ["hello world"]  # no identical re-edit (MESSAGE_NOT_MODIFIED)


async def test_deleted_message_is_dropped(processor, queue):
    queue.add(1, 10)
    assert await processor.process(1, 10) == "gone"
    assert queue.pending() == []


async def test_transient_error_is_retried_with_backoff(processor, telegram, transcriber, queue, sleep):
    telegram.add_voice(1, 10)
    transcriber.errors = [TransientError("503"), TimeoutError()]
    assert await processor.process(1, 10) == "done"
    assert sleep.delays == [10, 20]
    assert telegram.caption(1, 10) == "hello world"
    assert queue.pending() == []


async def test_unexpected_exception_is_retried_not_raised(processor, telegram, transcriber):
    telegram.add_voice(1, 10)
    transcriber.errors = [RuntimeError("bug")]
    assert await processor.process(1, 10) == "done"


async def test_unexpected_errors_stop_at_attempt_limit(processor, telegram, transcriber, queue, sleep):
    telegram.add_voice(1, 10)
    transcriber.errors = [RuntimeError("bug")] * 10
    assert await processor.process(1, 10) == "failed"
    assert transcriber.calls == 3
    assert sleep.delays == [10, 20, 40]
    assert telegram.caption(1, 10) == FAILURE_PREFIX + "gave up after 3 attempts"
    assert queue.pending() == []


class Clock:
    """Wall clock that moves only when the processor sleeps."""

    def __init__(self, sleep):
        self.start = time.time()
        self.sleep = sleep

    def __call__(self):
        return self.start + sum(self.sleep.delays)


async def test_outage_longer_than_the_attempt_budget_still_ends_in_a_transcription(telegram, transcriber, queue, sleep):
    # One hour of OpenAI 503s, then account problems: far more than max_attempts x backoff (150 s before).
    processor = VoiceProcessor(telegram, transcriber, queue, max_attempts=3, sleep=sleep, clock=Clock(sleep))
    telegram.add_voice(1, 10)
    transcriber.errors = [TransientError("OpenAI server error 503")] * 20 + [TransientError("OpenAI quota exceeded")] * 5
    assert await processor.process(1, 10) == "done"
    assert telegram.caption(1, 10) == "hello world"
    assert transcriber.calls == 26
    assert max(sleep.delays) == 300 and sum(sleep.delays) > 3600  # capped backoff, more than an hour in total
    assert queue.pending() == []


async def test_outage_beyond_the_retry_window_writes_the_note(telegram, transcriber, queue, sleep):
    processor = VoiceProcessor(telegram, transcriber, queue, retry_window=3600, sleep=sleep, clock=Clock(sleep))
    telegram.add_voice(1, 10)
    transcriber.errors = [TransientError("OpenAI server error 503")] * 100
    assert await processor.process(1, 10) == "failed"
    assert 3600 < sum(sleep.delays) < 3600 + 300
    assert telegram.caption(1, 10) == FAILURE_PREFIX + "gave up after 1 h of errors (OpenAI server error 503)"
    assert queue.pending() == []


async def test_outage_retries_do_not_use_up_crash_attempts(processor, telegram, transcriber, queue):
    telegram.add_voice(1, 10)
    transcriber.errors = [TransientError("down")] * 10
    assert await processor.process(1, 10) == "done"
    # A crash counter of 10 would have dropped the message after a restart.
    queue.add(1, 11)
    assert queue.start_attempt(1, 11).attempts == 1


async def test_shutdown_during_backoff_does_not_count_as_a_crash(telegram, transcriber, queue):
    async def shutdown(delay):
        raise asyncio.CancelledError

    processor = VoiceProcessor(telegram, transcriber, queue, max_attempts=1, sleep=shutdown)
    telegram.add_voice(1, 10)
    transcriber.errors = [TransientError("down")]
    with pytest.raises(asyncio.CancelledError):
        await processor.process(1, 10)
    assert [job.attempts for job in queue.pending()] == [0]
    assert await processor.process(1, 10) == "done"  # max_attempts=1 is not used up


async def test_failed_note_write_keeps_the_job_until_it_is_written(processor, telegram, transcriber, queue, sleep):
    telegram.add_voice(1, 10)
    transcriber.errors = [PermanentError("OpenAI error 400")]
    # The status caption goes through, then writing the note fails twice.
    telegram.errors["set_caption"] = [None, TransientError("Telegram server error"), OSError("network down")]
    assert await processor.process(1, 10) == "failed"
    assert sleep.delays == [10, 20]
    assert telegram.caption(1, 10) == FAILURE_PREFIX + "OpenAI error 400"
    assert queue.pending() == []


async def test_failed_note_write_survives_a_restart(telegram, transcriber, queue):
    async def shutdown(delay):
        raise asyncio.CancelledError

    telegram.add_voice(1, 10, caption=STATUS_TEXT)
    for _ in range(3):
        queue.start_attempt(1, 10)  # three crashes: the note is due
    telegram.errors["set_caption"] = [TransientError("Telegram server error")]
    processor = VoiceProcessor(telegram, transcriber, queue, max_attempts=3, sleep=shutdown)
    with pytest.raises(asyncio.CancelledError):
        await processor.process(1, 10)
    assert telegram.caption(1, 10) == STATUS_TEXT
    assert len(queue.pending()) == 1  # not lost: the next start writes the note

    processor = VoiceProcessor(telegram, transcriber, queue, max_attempts=3, sleep=shutdown)
    assert await processor.process(1, 10) == "failed"
    assert telegram.caption(1, 10) == FAILURE_PREFIX + "gave up after 3 attempts"
    assert transcriber.calls == 0 and queue.pending() == []


async def test_note_that_telegram_refuses_for_good_drops_the_job(processor, telegram, transcriber, queue):
    telegram.add_voice(1, 10)
    transcriber.errors = [PermanentError("OpenAI error 400")]
    calls = 0
    original = telegram.set_caption

    async def set_caption(chat_id, message_id, text, *, italic=False):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermanentError("Telegram error MESSAGE_EDIT_TIME_EXPIRED")
        await original(chat_id, message_id, text, italic=italic)

    telegram.set_caption = set_caption
    assert await processor.process(1, 10) == "failed"
    assert queue.pending() == []


async def test_permanent_error_writes_note_without_retry(processor, telegram, transcriber, queue, sleep):
    telegram.add_voice(1, 10)
    transcriber.errors = [PermanentError("OpenAI error 400")]
    assert await processor.process(1, 10) == "failed"
    assert transcriber.calls == 1 and sleep.delays == []
    assert telegram.caption(1, 10) == FAILURE_PREFIX + "OpenAI error 400"
    assert telegram.edits[-1][2] is True
    assert queue.pending() == []


async def test_flood_wait_sleeps_and_is_not_counted(processor, telegram, queue, sleep):
    telegram.add_voice(1, 10)
    telegram.errors["set_caption"] = [FloodWait(42)]
    assert await processor.process(1, 10) == "done"
    assert sleep.delays == [43]
    assert telegram.caption(1, 10) == "hello world"


async def test_attempt_limit_survives_crashes(processor, telegram, transcriber, queue):
    # Three earlier runs crashed the process mid-way; the counter is persistent.
    telegram.add_voice(1, 10, caption=STATUS_TEXT)
    for _ in range(3):
        queue.start_attempt(1, 10)
    assert await processor.process(1, 10) == "failed"
    assert transcriber.calls == 0
    assert telegram.caption(1, 10) == FAILURE_PREFIX + "gave up after 3 attempts"
    assert queue.pending() == []


async def test_too_large_file_is_not_downloaded(processor, telegram):
    telegram.add_voice(1, 10, size=MAX_FILE_SIZE + 1)
    assert await processor.process(1, 10) == "failed"
    assert telegram.downloads == 0
    assert telegram.caption(1, 10).startswith(FAILURE_PREFIX)


async def test_failure_note_does_not_overwrite_real_caption(processor, telegram, transcriber):
    telegram.add_voice(1, 10)

    async def user_captions_meanwhile(audio):
        telegram.messages[(1, 10)].caption = "typed by the user"
        raise PermanentError("x")

    transcriber.transcribe = user_captions_meanwhile
    assert await processor.process(1, 10) == "failed"
    assert telegram.caption(1, 10) == "typed by the user"


async def test_caption_too_long_replies_in_chunks(processor, telegram, transcriber):
    telegram.add_voice(1, 10)
    transcriber.text = ("word " * 2000).strip()  # ~10 000 chars
    assert await processor.process(1, 10) == "done"
    assert telegram.caption(1, 10) == SEE_BELOW_TEXT
    chunks = [text for _, text in telegram.replies]
    assert len(chunks) == 3 and all(len(c) <= 4096 for c in chunks)
    assert " ".join(chunks) == transcriber.text


async def test_caption_too_long_retry_does_not_repeat_sent_chunks(processor, telegram, transcriber):
    telegram.add_voice(1, 10)
    transcriber.text = ("word " * 2000).strip()
    original_reply = telegram.reply
    calls = 0

    async def flaky_reply(chat_id, message_id, text):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TransientError("network")
        await original_reply(chat_id, message_id, text)

    telegram.reply = flaky_reply
    assert await processor.process(1, 10) == "done"
    assert len(telegram.replies) == 3
    assert " ".join(text for _, text in telegram.replies) == transcriber.text
    assert transcriber.calls == 1  # the saved transcript is reused


async def test_restart_after_a_reply_failure_reuses_the_transcript_and_sent_chunks(telegram, queue):
    async def shutdown(delay):
        raise asyncio.CancelledError

    telegram.add_voice(1, 10)
    first = FakeTranscriber(("word " * 2000).strip())
    telegram.errors["reply"] = [None, TransientError("network")]
    with pytest.raises(asyncio.CancelledError):  # the bot is stopped during the backoff
        await VoiceProcessor(telegram, first, queue, sleep=shutdown).process(1, 10)
    assert len(telegram.replies) == 1 and telegram.caption(1, 10) == STATUS_TEXT

    # After the restart OpenAI would return a different text; it must not be asked again.
    second = FakeTranscriber("a different transcription")
    assert await VoiceProcessor(telegram, second, queue, sleep=shutdown).process(1, 10) == "done"
    assert second.calls == 0
    assert len(telegram.replies) == 3
    assert " ".join(text for _, text in telegram.replies) == first.text
    assert telegram.caption(1, 10) == SEE_BELOW_TEXT


async def test_empty_transcription_gets_marker(processor, telegram, transcriber):
    telegram.add_voice(1, 10)
    transcriber.text = ""
    assert await processor.process(1, 10) == "done"
    assert telegram.caption(1, 10) == NO_SPEECH_TEXT


async def test_fatal_error_keeps_job_queued(processor, telegram, queue):
    telegram.add_voice(1, 10)
    telegram.errors["fetch"] = [FatalError("session revoked")]
    with pytest.raises(FatalError):
        await processor.process(1, 10)
    assert [(job.chat_id, job.message_id, job.attempts) for job in queue.pending()] == [(1, 10, 0)]


async def test_processing_timeout_is_retried_like_an_outage(telegram, transcriber, queue, sleep):
    processor = VoiceProcessor(telegram, transcriber, queue, max_attempts=1, retry_window=100, timeout=0.01,
                               sleep=sleep, clock=Clock(sleep))
    telegram.add_voice(1, 10)
    transcriber.block = asyncio.Event()  # hangs forever
    assert await processor.process(1, 10) == "failed"
    assert transcriber.calls == 5  # 10 + 20 + 40 + 80 s of backoff > 100 s
    assert telegram.caption(1, 10) == FAILURE_PREFIX + "gave up after 100 s of errors (TimeoutError)"


def test_split_text():
    assert split_text("short") == ["short"]
    assert split_text("") == []
    assert split_text("aaaa bbbb cccc", limit=9) == ["aaaa bbbb", "cccc"]
    assert split_text("line one\nline two", limit=12) == ["line one", "line two"]
    assert split_text("x" * 10, limit=4) == ["xxxx", "xxxx", "xx"]
