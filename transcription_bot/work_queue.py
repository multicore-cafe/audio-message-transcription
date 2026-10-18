"""Durable queue of voice messages that still have to be transcribed, plus the bot's heartbeat."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_JOB_COLUMNS = "chat_id, message_id, attempts, added_at, text, sent_chunks"


@dataclass(frozen=True)
class Job:
    chat_id: int
    message_id: int
    attempts: int  # attempts that were started and did not end in a known way (crash, restart, bug)
    added_at: float
    text: str | None = None  # the transcript, once OpenAI returned it
    sent_chunks: int = 0  # reply chunks of a long transcript already sent


class WorkQueue:
    """SQLite table of jobs; every change is committed at once."""

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
            " text TEXT,"
            " sent_chunks INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (chat_id, message_id))"
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(jobs)")}
        if "text" not in columns:  # queue file of an earlier version
            self._db.execute("ALTER TABLE jobs ADD COLUMN text TEXT")
            self._db.execute("ALTER TABLE jobs ADD COLUMN sent_chunks INTEGER NOT NULL DEFAULT 0")
        self._db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value REAL NOT NULL)")

    def add(self, chat_id: int, message_id: int) -> None:
        """Record a job; a job that is already queued keeps its state."""
        self._db.execute(
            "INSERT OR IGNORE INTO jobs (chat_id, message_id, added_at) VALUES (?, ?, ?)",
            (chat_id, message_id, time.time()),
        )

    def start_attempt(self, chat_id: int, message_id: int) -> Job:
        """Count one more attempt before doing the work; `end_attempt` takes it back."""
        row = self._db.execute(
            "INSERT INTO jobs (chat_id, message_id, attempts, added_at) VALUES (?, ?, 1, ?)"
            " ON CONFLICT (chat_id, message_id) DO UPDATE SET attempts = attempts + 1"
            f" RETURNING {_JOB_COLUMNS}",
            (chat_id, message_id, time.time()),
        ).fetchone()
        return Job(*row)

    def end_attempt(self, chat_id: int, message_id: int) -> None:
        """The attempt ended in a known way (outage, shutdown): it does not count toward the limit."""
        self._update(chat_id, message_id, "attempts = MAX(attempts - 1, 0)")

    def save_text(self, chat_id: int, message_id: int, text: str) -> None:
        self._update(chat_id, message_id, "text = ?", text)

    def save_sent_chunks(self, chat_id: int, message_id: int, count: int) -> None:
        self._update(chat_id, message_id, "sent_chunks = ?", count)

    def _update(self, chat_id: int, message_id: int, assignment: str, *values) -> None:
        self._db.execute(f"UPDATE jobs SET {assignment} WHERE chat_id = ? AND message_id = ?",
                         (*values, chat_id, message_id))

    def remove(self, chat_id: int, message_id: int) -> None:
        self._db.execute("DELETE FROM jobs WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))

    def pending(self) -> list[Job]:
        rows = self._db.execute(f"SELECT {_JOB_COLUMNS} FROM jobs ORDER BY added_at, rowid")
        return [Job(*row) for row in rows]

    def heartbeat(self) -> float | None:
        """When the bot last confirmed it was connected and saw all earlier messages."""
        row = self._db.execute("SELECT value FROM state WHERE key = 'heartbeat'").fetchone()
        return row[0] if row else None

    def set_heartbeat(self, when: float) -> None:
        self._db.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('heartbeat', ?)", (when,))

    def close(self) -> None:
        self._db.close()
