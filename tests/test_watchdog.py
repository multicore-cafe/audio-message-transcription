import asyncio

import pytest

from tests.fakes import RecordingSleep
from transcription_bot.watchdog import Watchdog


def test_exit_decision_needs_consecutive_failures():
    dog = Watchdog(lambda: None, max_failures=3)
    assert [dog.record(ok) for ok in (False, False, True, False, False)] == [False] * 5
    assert dog.record(False) is True


async def test_run_returns_after_consecutive_failures_and_timeouts():
    results = iter(["fail", "ok", "fail", "hang", "fail"])

    async def check():
        result = next(results)
        if result == "fail":
            raise ConnectionError("down")
        if result == "hang":
            await asyncio.Event().wait()

    sleep = RecordingSleep()
    dog = Watchdog(check, interval=60, timeout=0.01, max_failures=3, sleep=sleep)
    await asyncio.wait_for(dog.run(), 5)
    assert sleep.delays == [60] * 5


async def test_run_keeps_going_while_checks_pass():
    calls = 0

    async def check():
        nonlocal calls
        calls += 1

    dog = Watchdog(check, interval=0, timeout=1, max_failures=1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(dog.run(), 0.05)
    assert calls > 1
