import pytest

from tests.fakes import FakeTelegram, FakeTranscriber, RecordingSleep


@pytest.fixture(autouse=True)
def _enable_mcp_by_default(monkeypatch):
    """The MCP service is off unless TELEGRAM_MCP_ENABLED=1; tests enable it like a real run does."""
    monkeypatch.setenv("TELEGRAM_MCP_ENABLED", "1")
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
