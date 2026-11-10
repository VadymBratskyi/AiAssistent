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

CREATE TABLE IF NOT EXISTS allowed_users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    fire_at    INTEGER NOT NULL,           -- unix seconds
    text       TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    fired      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_fire_at
    ON reminders(fired, fire_at);

CREATE TABLE IF NOT EXISTS team_questions (
    id          TEXT PRIMARY KEY,           -- uuid4 hex, referenced by orchestrator
    team_id     TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,           -- куди надіслано питання
    message_id  INTEGER,                    -- id повідомлення-питання в Telegram
    text        TEXT NOT NULL,
    answer      TEXT,
    created_at  INTEGER NOT NULL,
    answered_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_team_questions_msg
    ON team_questions(chat_id, message_id);

CREATE TABLE IF NOT EXISTS team_inbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id      TEXT NOT NULL,
    text         TEXT NOT NULL,
    from_user_id INTEGER,
    created_at   INTEGER NOT NULL,
    delivered_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_team_inbox_pending
    ON team_inbox(team_id, delivered_at);
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

    # ---- user allowlist -------------------------------------------------

    def is_user_allowed(self, user_id: int) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM allowed_users WHERE user_id=?", (user_id,)
            ).fetchone()
        return row is not None

    def is_user_admin(self, user_id: int) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT is_admin FROM allowed_users WHERE user_id=?", (user_id,)
            ).fetchone()
        return bool(row and row[0])

    def add_user(self, user_id: int, username: str | None = None, is_admin: bool = False) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO allowed_users(user_id, username, is_admin, created_at) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "  username=COALESCE(excluded.username, allowed_users.username), "
                "  is_admin=excluded.is_admin",
                (user_id, username, 1 if is_admin else 0, int(time.time())),
            )

    def update_username(self, user_id: int, username: str | None) -> None:
        if not username:
            return
        with self._conn() as c:
            c.execute(
                "UPDATE allowed_users SET username=? WHERE user_id=? AND (username IS NULL OR username != ?)",
                (username, user_id, username),
            )

    def remove_user(self, user_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM allowed_users WHERE user_id=?", (user_id,))
            return cur.rowcount > 0

    def list_users(self) -> list[tuple[int, str | None, bool, int]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT user_id, username, is_admin, created_at "
                "FROM allowed_users ORDER BY created_at",
            ).fetchall()
        return [(uid, name, bool(adm), ts) for uid, name, adm, ts in rows]

    def count_users(self) -> int:
        with self._conn() as c:
            (n,) = c.execute("SELECT COUNT(*) FROM allowed_users").fetchone()
        return n

    # ---- reminders ------------------------------------------------------

    def add_reminder(self, user_id: int, fire_at: int, text: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO reminders(user_id, fire_at, text, created_at) VALUES(?, ?, ?, ?)",
                (user_id, fire_at, text.strip(), int(time.time())),
            )
            return cur.lastrowid

    def due_reminders(self, now: int) -> list[tuple[int, int, str]]:
        """Return (id, user_id, text) for reminders whose fire_at <= now and
        not yet fired."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, user_id, text FROM reminders "
                "WHERE fired=0 AND fire_at<=? ORDER BY fire_at",
                (now,),
            ).fetchall()
        return [(rid, uid, txt) for rid, uid, txt in rows]

    def mark_reminder_fired(self, reminder_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE reminders SET fired=1 WHERE id=?", (reminder_id,))

    def list_upcoming_reminders(self, user_id: int) -> list[tuple[int, int, str]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, fire_at, text FROM reminders "
                "WHERE user_id=? AND fired=0 ORDER BY fire_at",
                (user_id,),
            ).fetchall()
        return [(rid, fa, txt) for rid, fa, txt in rows]

    def cancel_reminder(self, reminder_id: int, user_id: int) -> bool:
        """Delete only if owned by user_id, returns True if a row was removed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM reminders WHERE id=? AND user_id=? AND fired=0",
                (reminder_id, user_id),
            )
            return cur.rowcount > 0

    # ---- team orchestrator questions ------------------------------------

    def add_team_question(
        self, qid: str, team_id: str, chat_id: int, message_id: int | None, text: str
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO team_questions(id, team_id, chat_id, message_id, text, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (qid, team_id, chat_id, message_id, text, int(time.time())),
            )

    def set_team_question_message_id(self, qid: str, message_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE team_questions SET message_id=? WHERE id=?", (message_id, qid)
            )

    def answer_team_question(self, qid: str, answer: str) -> bool:
        """Set answer if not already answered. Returns True if this call set it."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE team_questions SET answer=?, answered_at=? "
                "WHERE id=? AND answer IS NULL",
                (answer, int(time.time()), qid),
            )
            return cur.rowcount > 0

    def get_team_question(self, qid: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, team_id, chat_id, message_id, text, answer, created_at, answered_at "
                "FROM team_questions WHERE id=?",
                (qid,),
            ).fetchone()
        if not row:
            return None
        keys = ["id", "team_id", "chat_id", "message_id", "text", "answer", "created_at", "answered_at"]
        return dict(zip(keys, row))

    def enqueue_team_inbox(self, team_id: str, text: str, from_user_id: int | None) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO team_inbox(team_id, text, from_user_id, created_at) VALUES(?,?,?,?)",
                (team_id, text, from_user_id, int(time.time())),
            )
            return cur.lastrowid

    def fetch_team_inbox(self, team_id: str, limit: int = 20) -> list[dict]:
        """Атомарно забрати всі недоставлені повідомлення команди і маркувати їх."""
        now = int(time.time())
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT id, text, from_user_id, created_at FROM team_inbox "
                "WHERE team_id=? AND delivered_at IS NULL "
                "ORDER BY id LIMIT ?",
                (team_id, limit),
            ).fetchall()
            if not rows:
                c.execute("COMMIT")
                return []
            ids = [r[0] for r in rows]
            placeholders = ",".join("?" * len(ids))
            c.execute(
                f"UPDATE team_inbox SET delivered_at=? WHERE id IN ({placeholders})",
                [now, *ids],
            )
            c.execute("COMMIT")
        return [
            {"id": rid, "text": txt, "from_user_id": uid, "created_at": ts}
            for rid, txt, uid, ts in rows
        ]

    def find_team_question_by_message(self, chat_id: int, message_id: int) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM team_questions WHERE chat_id=? AND message_id=? AND answer IS NULL",
                (chat_id, message_id),
            ).fetchone()
        return row[0] if row else None
