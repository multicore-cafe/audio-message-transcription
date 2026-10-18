"""Periodic liveness check of the Telegram connection."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)


class Watchdog:
    """`run()` returns once `max_failures` checks in a row have failed or timed out."""

    def __init__(
        self,
        check: Callable[[], Awaitable[Any]],
        *,
        interval: float = 60,
        timeout: float = 30,
        max_failures: int = 3,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self._check = check
        self.interval = interval
        self.timeout = timeout
        self.max_failures = max_failures
        self._sleep = sleep
        self.failures = 0

    def record(self, ok: bool) -> bool:
        """Register a check result; True means the process should exit."""
        self.failures = 0 if ok else self.failures + 1
        return self.failures >= self.max_failures

    async def run(self) -> None:
        while True:
            await self._sleep(self.interval)
            try:
                await asyncio.wait_for(self._check(), self.timeout)
                ok = True
            except Exception as exc:
                log.warning("watchdog: Telegram check failed (%d in a row): %r", self.failures + 1, exc)
                ok = False
            if self.record(ok):
                log.error("watchdog: %d checks in a row failed, Telegram connection is dead", self.failures)
                return
