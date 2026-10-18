"""Durable queue of voice messages that still have to be transcribed."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Job:
    chat_id: int
    message_id: int
    attempts: int


class WorkQueue:
    """SQLite table of (chat_id, message_id, attempts); every change is committed at once."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            " chat_id INTEGER NOT NULL,"
            " message_id INTEGER NOT NULL,"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " added_at REAL NOT NULL,"
            " PRIMARY KEY (chat_id, message_id))"
        )

    def add(self, chat_id: int, message_id: int) -> bool:
        """Record a job; returns False if it is already queued."""
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO jobs (chat_id, message_id, added_at) VALUES (?, ?, ?)",
            (chat_id, message_id, time.time()),
        )
        return cursor.rowcount == 1

    def start_attempt(self, chat_id: int, message_id: int) -> int:
        """Count one more attempt before doing the work; returns the attempt number."""
        (attempts,) = self._db.execute(
            "INSERT INTO jobs (chat_id, message_id, attempts, added_at) VALUES (?, ?, 1, ?)"
            " ON CONFLICT (chat_id, message_id) DO UPDATE SET attempts = attempts + 1"
            " RETURNING attempts",
            (chat_id, message_id, time.time()),
        ).fetchone()
        return attempts

    def remove(self, chat_id: int, message_id: int) -> None:
        self._db.execute("DELETE FROM jobs WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))

    def pending(self) -> list[Job]:
        rows = self._db.execute("SELECT chat_id, message_id, attempts FROM jobs ORDER BY added_at, rowid")
        return [Job(*row) for row in rows]

    def close(self) -> None:
        self._db.close()
