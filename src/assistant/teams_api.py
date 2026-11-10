"""Localhost HTTP API, через який оркестратор-сесія Claude Code спілкується з ботом.

Три ендпоінти, всі вимагають `Authorization: Bearer <ORCHESTRATOR_TOKEN>`:

- POST /teams/{team}/notify       → надіслати нотифікацію в чат команди
- POST /teams/{team}/ask          → поставити питання, повертає {question_id}
- GET  /teams/{team}/answer/{id}  → long-poll відповіді (до 25с, 204 поки нема)

Прив'язаний до 127.0.0.1 за замовчуванням, щоб не світити назовні.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Awaitable, Callable

from aiohttp import web

from .config import Config
from .db import Db

log = logging.getLogger(__name__)

# question_id → asyncio.Event. Подія ставиться, коли користувач відповів у Telegram.
# Live-only: втрачається при рестарті, тож /answer після рестарту віддасть
# відповідь з БД (якщо вона є) або 404.
_WAITERS: dict[str, asyncio.Event] = {}
# team_id → Event, яка ставиться коли в inbox цієї команди з'являється повідомлення.
_INBOX_WAITERS: dict[str, asyncio.Event] = {}

LONG_POLL_TIMEOUT = 25.0


def notify_question_answered(qid: str) -> None:
    """Викликається з telegram-хендлера коли користувач дав відповідь."""
    ev = _WAITERS.get(qid)
    if ev is not None:
        ev.set()


def notify_inbox(team_id: str) -> None:
    """Викликається коли бот поклав нове повідомлення в чергу команди."""
    ev = _INBOX_WAITERS.get(team_id)
    if ev is not None:
        ev.set()
        ev.clear()


SendNotifyFn = Callable[[int, str, str | None], Awaitable[int]]
# (chat_id, text, parse_mode) → message_id

SendQuestionFn = Callable[[int, str, list[str] | None, str], Awaitable[int]]
# (chat_id, text, options, qid) → message_id


def build_app(
    config: Config,
    db: Db,
    send_notify: SendNotifyFn,
    send_question: SendQuestionFn,
) -> web.Application:
    app = web.Application()
    token = config.orchestrator_token

    @web.middleware
    async def auth_mw(request: web.Request, handler):  # type: ignore[no-untyped-def]
        if token:
            header = request.headers.get("Authorization", "")
            if header != f"Bearer {token}":
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    app.middlewares.append(auth_mw)

    def _resolve_chat(team_id: str) -> int | None:
        team = config.teams.get(team_id)
        if team is None:
            return None
        if team.notify_chat_id is not None:
            return team.notify_chat_id
        # fallback — перший admin з allowlist
        for user_id, _u, is_admin, _ts in db.list_users():
            if is_admin:
                return user_id
        return None

    async def notify(request: web.Request) -> web.Response:
        team_id = request.match_info["team"]
        if team_id not in config.teams:
            return web.json_response({"error": f"unknown team: {team_id}"}, status=404)
        chat_id = _resolve_chat(team_id)
        if chat_id is None:
            return web.json_response(
                {"error": "no notify_chat_id configured and no admin to fallback to"},
                status=503,
            )
        body: dict[str, Any] = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "text is required"}, status=400)
        parse_mode = body.get("parse_mode")
        prefixed = f"[{team_id}] {text}"
        try:
            msg_id = await send_notify(chat_id, prefixed, parse_mode)
        except Exception as e:
            log.exception("notify failed")
            return web.json_response({"error": str(e)}, status=502)
        return web.json_response({"ok": True, "message_id": msg_id, "chat_id": chat_id})

    async def ask(request: web.Request) -> web.Response:
        team_id = request.match_info["team"]
        if team_id not in config.teams:
            return web.json_response({"error": f"unknown team: {team_id}"}, status=404)
        chat_id = _resolve_chat(team_id)
        if chat_id is None:
            return web.json_response({"error": "no chat_id to send to"}, status=503)
        body: dict[str, Any] = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "text is required"}, status=400)
        options = body.get("options")
        if options is not None and (
            not isinstance(options, list) or not all(isinstance(o, str) for o in options)
        ):
            return web.json_response({"error": "options must be list[str]"}, status=400)

        qid = secrets.token_hex(12)
        db.add_team_question(qid, team_id, chat_id, None, text)
        _WAITERS[qid] = asyncio.Event()
        try:
            msg_id = await send_question(chat_id, f"[{team_id}] ❓ {text}", options, qid)
        except Exception as e:
            _WAITERS.pop(qid, None)
            log.exception("ask send failed")
            return web.json_response({"error": str(e)}, status=502)
        db.set_team_question_message_id(qid, msg_id)
        return web.json_response({"question_id": qid, "message_id": msg_id})

    async def answer(request: web.Request) -> web.Response:
        team_id = request.match_info["team"]
        qid = request.match_info["qid"]
        row = db.get_team_question(qid)
        if row is None or row["team_id"] != team_id:
            return web.json_response({"error": "question not found"}, status=404)
        if row["answer"] is not None:
            return web.json_response(
                {"question_id": qid, "answer": row["answer"], "answered_at": row["answered_at"]}
            )
        ev = _WAITERS.get(qid)
        if ev is None:
            ev = asyncio.Event()
            _WAITERS[qid] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout=LONG_POLL_TIMEOUT)
        except asyncio.TimeoutError:
            return web.Response(status=204)
        row = db.get_team_question(qid)
        if row is None or row["answer"] is None:
            return web.Response(status=204)
        return web.json_response(
            {"question_id": qid, "answer": row["answer"], "answered_at": row["answered_at"]}
        )

    async def inbox(request: web.Request) -> web.Response:
        team_id = request.match_info["team"]
        if team_id not in config.teams:
            return web.json_response({"error": f"unknown team: {team_id}"}, status=404)
        msgs = db.fetch_team_inbox(team_id)
        if msgs:
            return web.json_response({"messages": msgs})
        ev = _INBOX_WAITERS.setdefault(team_id, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout=LONG_POLL_TIMEOUT)
        except asyncio.TimeoutError:
            return web.Response(status=204)
        msgs = db.fetch_team_inbox(team_id)
        if not msgs:
            return web.Response(status=204)
        return web.json_response({"messages": msgs})

    app.router.add_post("/teams/{team}/notify", notify)
    app.router.add_post("/teams/{team}/ask", ask)
    app.router.add_get("/teams/{team}/answer/{qid}", answer)
    app.router.add_get("/teams/{team}/inbox", inbox)
    app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
    return app


async def start_server(
    config: Config,
    db: Db,
    send_notify: SendNotifyFn,
    send_question: SendQuestionFn,
) -> web.AppRunner:
    app = build_app(config, db, send_notify, send_question)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.orchestrator_host, config.orchestrator_port)
    await site.start()
    log.info(
        "team orchestrator API listening on http://%s:%d",
        config.orchestrator_host,
        config.orchestrator_port,
    )
    return runner
