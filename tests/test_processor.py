import asyncio

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
from transcription_bot.transcriber import MAX_FILE_SIZE
from transcription_bot.work_queue import Job


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


@pytest.mark.parametrize("caption", [STATUS_TEXT, "Text recognizing..."])
async def test_retries_message_left_with_status_caption(processor, telegram, caption):
    telegram.add_voice(1, 10, caption=caption)
    assert await processor.process(1, 10) == "done"
    assert telegram.caption(1, 10) == "hello world"
    if caption == STATUS_TEXT:  # no identical re-edit (MESSAGE_NOT_MODIFIED)
        assert [text for _, text, _ in telegram.edits] == ["hello world"]


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


async def test_transient_errors_stop_at_attempt_limit(processor, telegram, transcriber, queue, sleep):
    telegram.add_voice(1, 10)
    transcriber.errors = [TransientError("down")] * 10
    assert await processor.process(1, 10) == "failed"
    assert transcriber.calls == 3
    assert sleep.delays == [10, 20]
    assert telegram.caption(1, 10) == FAILURE_PREFIX + "gave up after 3 attempts"
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
    assert queue.pending() == [Job(1, 10, 1)]


async def test_processing_timeout_is_transient(telegram, transcriber, queue, sleep):
    processor = VoiceProcessor(telegram, transcriber, queue, max_attempts=2, timeout=0.01, sleep=sleep)
    telegram.add_voice(1, 10)
    transcriber.block = asyncio.Event()  # hangs forever
    assert await processor.process(1, 10) == "failed"
    assert transcriber.calls == 2


def test_split_text():
    assert split_text("short") == ["short"]
    assert split_text("") == []
    assert split_text("aaaa bbbb cccc", limit=9) == ["aaaa bbbb", "cccc"]
    assert split_text("line one\nline two", limit=12) == ["line one", "line two"]
    assert split_text("x" * 10, limit=4) == ["xxxx", "xxxx", "xx"]
