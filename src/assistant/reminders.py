from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .db import Db

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


class RemindersScheduler:
    """Polls the reminders table and fires due ones via a `push` callable."""

    def __init__(
        self,
        db: Db,
        push: Callable[[int, str], Awaitable[None]],
        interval: int = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.db = db
        self.push = push
        self.interval = interval
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="reminders")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        log.info("reminders scheduler started (poll every %ds)", self.interval)
        while True:
            try:
                now = int(time.time())
                due = self.db.due_reminders(now)
                for rid, user_id, text in due:
                    try:
                        await self.push(user_id, f"⏰ Нагадування:\n{text}")
                    except Exception:
                        log.exception("failed to push reminder %s to %s", rid, user_id)
                        continue
                    self.db.mark_reminder_fired(rid)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reminders iteration failed")
            await asyncio.sleep(self.interval)


__all__ = ["RemindersScheduler"]
