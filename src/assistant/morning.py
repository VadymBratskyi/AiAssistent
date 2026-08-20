from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

KYIV = ZoneInfo("Europe/Kyiv")
FIRE_AT = time(hour=8, minute=30)
WEEKDAYS = {0, 1, 2, 3, 4}  # Mon..Fri

BRIEF_PROMPT = (
    "Це проактивний ранковий бриф. Виклич відповідні інструменти і склади коротке зведення:\n"
    "1. Зустрічі на сьогодні (Microsoft 365 → outlook_calendar_search): час (київський), "
    "тема, для Teams-online додай посилання на приєднання.\n"
    "2. Топ-3 останні непрочитані листи з Outlook (Microsoft 365 → outlook_email_search "
    "з фільтром isRead=false): відправник, тема, коротка суть в 1 реченні.\n"
    "3. Мої Jira задачі In Progress (Atlassian → getAccessibleAtlassianResources для "
    'cloudId, потім JQL: assignee = currentUser() AND status = "In Progress"): '
    "ключ, summary, статус.\n"
    "4. Якщо в пам'яті або переписці є щось про мої обіцянки — коротко нагадай.\n\n"
    "Формат: чіткі секції з emoji-заголовками, для Telegram, коротко. "
    "На початку — привітання одним словом (\"Доброго ранку 👋\")."
)


class MorningBrief:
    """Fires a proactive brief at 08:30 Kyiv on weekdays.

    Skips silently if credentials are expired (credmon already alerts on that).
    """

    def __init__(
        self,
        push: Callable[[str], Awaitable[None]],
        get_agent_reply: Callable[[str], Awaitable[str]],
        get_creds_state: Callable[[], str] | None = None,
    ) -> None:
        self.push = push
        self.get_agent_reply = get_agent_reply
        self.get_creds_state = get_creds_state
        self._task: asyncio.Task | None = None
        # Guard against double-firing if wake-up wobble puts us into the same
        # minute twice.
        self._last_fired_date: str | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="morning-brief")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        log.info("morning brief scheduler started (08:30 Kyiv, Mon-Fri)")
        while True:
            try:
                sleep_s = self._seconds_until_next_fire()
                log.info("next morning brief in %.0fs", sleep_s)
                await asyncio.sleep(sleep_s)
                await self._fire_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("morning brief iteration failed")
                # Avoid a tight loop if something is fundamentally broken.
                await asyncio.sleep(60)

    def _seconds_until_next_fire(self) -> float:
        now = datetime.now(KYIV)
        target = now.replace(hour=FIRE_AT.hour, minute=FIRE_AT.minute, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        # Skip weekends.
        while target.weekday() not in WEEKDAYS:
            target = target + timedelta(days=1)
        return (target - now).total_seconds()

    async def _fire_once(self) -> None:
        today = datetime.now(KYIV).strftime("%Y-%m-%d")
        if self._last_fired_date == today:
            return
        self._last_fired_date = today

        if self.get_creds_state and self.get_creds_state() != "healthy":
            log.info("morning brief skipped: creds not healthy")
            await self.push(
                "🌅 Ранковий бриф пропущено — сесія Claude не активна. "
                "Виконай /login на хості, і завтра відновиться."
            )
            return

        try:
            reply = await self.get_agent_reply(BRIEF_PROMPT)
        except Exception:
            log.exception("morning brief agent call failed")
            await self.push("🌅 Ранковий бриф не вдалось скласти — див. логи.")
            return

        await self.push(reply)


__all__ = ["MorningBrief", "BRIEF_PROMPT"]
