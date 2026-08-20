from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable, Awaitable

log = logging.getLogger(__name__)

DEFAULT_CREDS_PATH = Path("/home/assistant/.claude/.credentials.json")


def read_expires_at(path: Path = DEFAULT_CREDS_PATH) -> int | None:
    """Return the OAuth access_token expiresAt (unix ms) or None if unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth") or {}
    exp = oauth.get("expiresAt")
    return int(exp) if isinstance(exp, (int, float)) else None


class CredentialsMonitor:
    """Watches ~/.claude/.credentials.json for expiry transitions and notifies.

    Uses coarse "expired vs healthy" states so we notify on the transition,
    not every poll. Rechecks the file on disk each cycle because the IDE
    plugin may refresh (and rewrite) it out-of-band.
    """

    def __init__(
        self,
        notify: Callable[[str], Awaitable[None]],
        path: Path = DEFAULT_CREDS_PATH,
        interval_seconds: int = 300,
    ) -> None:
        self.notify = notify
        self.path = path
        self.interval = interval_seconds
        self._state: str | None = None  # "healthy" | "expired" | "missing"
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="credmon")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # Threshold for "clearly invalidated": Y2K in ms. Anything below this
    # was set by a failed refresh (usually 0 or a very small integer), which
    # is the actual "broken, needs /login" state. A legitimately-close
    # expiresAt just means the CLI is about to refresh — not our problem.
    _INVALIDATED_BEFORE_MS = 946_684_800_000  # 2000-01-01 UTC

    def _classify(self) -> str:
        if not self.path.exists():
            return "missing"
        exp = read_expires_at(self.path)
        if exp is None or exp < self._INVALIDATED_BEFORE_MS:
            return "expired"
        return "healthy"

    async def _loop(self) -> None:
        log.info("credmon started, path=%s interval=%ds", self.path, self.interval)
        while True:
            try:
                new_state = self._classify()
                if new_state != self._state:
                    log.info("credmon state: %s → %s", self._state, new_state)
                    # Only notify AFTER first classification so we don't spam
                    # on startup when we don't yet know the previous state.
                    if self._state is not None and new_state in {"expired", "missing"}:
                        await self.notify(self._message(new_state))
                    elif self._state in {"expired", "missing"} and new_state == "healthy":
                        await self.notify("✅ Авторизація Claude оновлена — бот знову працює як зазвичай.")
                    self._state = new_state
            except Exception:
                log.exception("credmon iteration failed")
            await asyncio.sleep(self.interval)

    @staticmethod
    def _message(state: str) -> str:
        if state == "missing":
            return (
                "⚠️ Файл авторизації Claude зник. Виконай у терміналі на хості:\n"
                "  claude\n"
                "  /login"
            )
        return (
            "⚠️ Сесія Claude протермінувалась. Щоб відновити роботу з "
            "пошти/Jira/wiki — у терміналі на хості:\n"
            "  claude\n"
            "  /login"
        )


__all__ = ["CredentialsMonitor", "read_expires_at", "DEFAULT_CREDS_PATH"]
