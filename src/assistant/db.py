from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

Role = Literal["user", "assistant"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    chat_id    INTEGER PRIMARY KEY,
    session_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL CHECK (role IN ('user','assistant')),
    content    TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES threads(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_id_id ON messages(chat_id, id);
"""


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


class Db:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def ensure_thread(self, chat_id: int) -> None:
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                "INSERT INTO threads(chat_id, created_at, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET updated_at=excluded.updated_at",
                (chat_id, now, now),
            )

    def append_message(self, chat_id: int, role: Role, content: str) -> None:
        self.ensure_thread(chat_id)
        with self._conn() as c:
            c.execute(
                "INSERT INTO messages(chat_id, role, content, created_at) VALUES(?, ?, ?, ?)",
                (chat_id, role, content, int(time.time())),
            )

    def load_history(self, chat_id: int, limit: int = 40) -> list[Message]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT role, content FROM messages WHERE chat_id=? "
                "ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [Message(role=r, content=c) for r, c in reversed(rows)]

    def clear_thread(self, chat_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            c.execute("UPDATE threads SET session_id=NULL WHERE chat_id=?", (chat_id,))

    def get_session_id(self, chat_id: int) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT session_id FROM threads WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return row[0] if row and row[0] else None

    def set_session_id(self, chat_id: int, session_id: str) -> None:
        self.ensure_thread(chat_id)
        with self._conn() as c:
            c.execute(
                "UPDATE threads SET session_id=?, updated_at=? WHERE chat_id=?",
                (session_id, int(time.time()), chat_id),
            )
