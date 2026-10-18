import pytest

from tests.fakes import FakeTelegram, FakeTranscriber, RecordingSleep
from transcription_bot.processor import VoiceProcessor
from transcription_bot.work_queue import WorkQueue


@pytest.fixture
def queue(tmp_path):
    q = WorkQueue(tmp_path / "queue.sqlite3")
    yield q
    q.close()


@pytest.fixture
def telegram():
    return FakeTelegram()


@pytest.fixture
def transcriber():
    return FakeTranscriber()


@pytest.fixture
def sleep():
    return RecordingSleep()


@pytest.fixture
def processor(telegram, transcriber, queue, sleep):
    return VoiceProcessor(telegram, transcriber, queue, max_attempts=3, retry_base_delay=10, sleep=sleep)
