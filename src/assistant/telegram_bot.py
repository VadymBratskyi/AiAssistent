from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agent import Agent
from .config import Config
from .credmon import CredentialsMonitor
from .db import Db
from .host_helper import HostHelper, HostHelperError
from .memory import Memory
from .morning import BRIEF_PROMPT, MorningBrief
from .stt import Transcriber

log = logging.getLogger(__name__)

# Prompt templates. Atlassian cloudId is NOT hardcoded — the agent discovers
# it via getAccessibleAtlassianResources so switching Jira site (via
# /mcp → re-auth with a different "Use app on") requires no bot changes.
PROMPT_MY = (
    "Спершу виклич Atlassian getAccessibleAtlassianResources щоб дізнатись cloudId. "
    'Потім знайди мої задачі в роботі JQL-ом: assignee = currentUser() AND status = "In Progress". '
    "Виведи стисло для Telegram: для кожної задачі — ключ, summary, статус і пряме посилання. "
    "Якщо задач немає — так і скажи одним рядком."
)
PROMPT_FREE = (
    "Спершу виклич Atlassian getAccessibleAtlassianResources щоб дізнатись cloudId. "
    'Потім знайди вільні dev-задачі JQL-ом: status = "Selected for Development" AND labels = Dev. '
    "Виведи стисло для Telegram: для кожної задачі — ключ, summary, статус і пряме посилання. "
    "Якщо задач немає — так і скажи одним рядком."
)
PROMPT_MAIL = (
    "Виклич Microsoft 365 MCP (outlook_email_search) і покажи 10 останніх листів з моєї "
    "вхідної пошти. Для кожного: відправник, тема, час (київський), і познач непрочитані. "
    "Стисло для Telegram, без зайвого тексту."
)
PROMPT_TODAY = (
    "Виклич Microsoft 365 MCP (outlook_calendar_search) і покажи мої зустрічі на сьогодні. "
    "Для кожної: час (київський), тема, і чи це Teams-онлайн (з посиланням на приєднання, якщо є). "
    "Стисло для Telegram. Якщо зустрічей немає — скажи одним рядком."
)
PROMPT_WEEK = (
    "Виклич Microsoft 365 MCP (outlook_calendar_search) і покажи мої зустрічі на найближчі 7 днів. "
    "Згрупуй за днями; для кожної: час (київський), тема, познач Teams-онлайн. "
    "Скасовані не показуй. Стисло для Telegram."
)


SCREENSHOTS_DIR = Path("/tmp/assistant-screenshots")
INBOX_DIR = Path("/tmp/assistant-inbox")


async def _flush_screenshots(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Send any new Playwright screenshots into the chat, then delete them."""
    if not SCREENSHOTS_DIR.exists():
        return
    for path in sorted(SCREENSHOTS_DIR.iterdir()):
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue
        try:
            with path.open("rb") as fh:
                await ctx.bot.send_photo(chat_id=chat_id, photo=fh)
        except Exception:
            log.exception("send_photo failed for %s", path)
        finally:
            try:
                path.unlink()
            except OSError:
                pass


async def _react(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, emoji: str) -> None:
    """Set a single-emoji reaction on the user's message. Best-effort — never raises."""
    try:
        await ctx.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji)],
        )
    except Exception:
        log.debug("set_message_reaction failed", exc_info=True)


def _gate(ctx: ContextTypes.DEFAULT_TYPE, update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    from .db import Db

    db: Db = ctx.application.bot_data["db"]
    if not db.is_user_allowed(user.id):
        log.warning(
            "rejected message from non-allowlisted user_id=%s username=%s",
            user.id,
            user.username,
        )
        return False
    # Keep username fresh — useful for /users display and audit.
    db.update_username(user.id, user.username)
    return True


def _is_admin(ctx: ContextTypes.DEFAULT_TYPE, update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    from .db import Db

    db: Db = ctx.application.bot_data["db"]
    return db.is_user_admin(user.id)


async def _cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    if not _gate(ctx, update):
        await update.effective_message.reply_text(
            f"Цей бот приватний. Твій Telegram user_id: {user.id}"
            + (f" (@{user.username})" if user.username else "")
            + ".\nПопроси адміністратора надати доступ: /grant " + str(user.id)
        )
        return
    await update.effective_message.reply_text(
        "Привіт, Вадімич. Я на зв'язку.\n\n"
        "Команди (в меню зліва від поля вводу):\n"
        "/my — мої задачі в роботі\n"
        "/free — вільні dev-задачі (Selected for Development)\n"
        "/mail — останні листи\n"
        "/today — зустрічі сьогодні\n"
        "/week — зустрічі на тиждень\n"
        "/remember, /memory, /forget — довгострокова пам'ять\n"
        "/reset — очистити контекст розмови\n\n"
        "Пиши/говори природною мовою — Jira, пошта й календар підключені. "
        "Після відповіді я можу запропонувати запам'ятати важливий факт про тебе.",
        reply_markup=ReplyKeyboardRemove(),
    )


LOGIN_HELP = (
    "🔐 Це не команда бота — це команда `claude` CLI на хості.\n\n"
    "Щоб оновити авторизацію Claude (яку використовують пошта / Jira / wiki):\n"
    "1. Відкрий термінал на своєму ПК\n"
    "2. Виконай `claude`\n"
    "3. Всередині CLI набери `/login`\n"
    "4. Пройди вхід у браузері\n\n"
    "Готово — бот сам підхопить свіжий токен через 10-20 секунд.\n\n"
    "_Чому це трапляється: IDE-плагін і бот ділять один OAuth-файл, "
    "і при паралельному refresh Anthropic ротує токен так, що один із "
    "клієнтів залишається зі старим. Не наш баг, обхід — раз на день перелогінитись._"
)


async def _cmd_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _gate(ctx, update):
        return
    await update.effective_message.reply_text(LOGIN_HELP, parse_mode="Markdown")


def _reauth_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔐 Re-auth зараз", callback_data="reauth")]]
    )


async def _trigger_reauth(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    helper: HostHelper = ctx.application.bot_data["host_helper"]
    if not helper.available:
        await ctx.bot.send_message(
            chat_id,
            "Host-helper не налаштований. Постав його один раз:\n"
            "`powershell -ExecutionPolicy Bypass -File deploy\\install-host-helper.ps1`\n"
            "І додай HOST_HELPER_URL / HOST_HELPER_TOKEN у .env.",
            parse_mode="Markdown",
        )
        return
    try:
        await helper.trigger_claude_login()
    except HostHelperError as e:
        await ctx.bot.send_message(chat_id, f"❌ {e}")
        return
    await ctx.bot.send_message(
        chat_id,
        "🚀 Запустив `claude login` на твоєму ПК. Терміналі має відкритись — пройди SSO+MFA у браузері.\n"
        "Коли токен оновиться, я сам повідомлю.",
    )


async def _cmd_reauth(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _gate(ctx, update):
        return
    await _trigger_reauth(ctx, update.effective_chat.id)


async def _on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except Exception:
        pass
    if not _gate(ctx, update):
        return
    data = query.data or ""
    if data == "reauth":
        await _trigger_reauth(ctx, query.message.chat_id)
        return
    if data.startswith("mem:"):
        await _handle_memory_callback(ctx, query, data)
        return


async def _handle_memory_callback(ctx: ContextTypes.DEFAULT_TYPE, query, data: str) -> None:
    _, decision, cid = data.split(":", 2)
    pending: dict = ctx.application.bot_data["mem_pending"]
    candidate = pending.pop(cid, None)
    if candidate is None:
        try:
            await query.edit_message_text("⌛ Пропозиція вже застаріла.")
        except Exception:
            pass
        return
    if decision == "yes":
        memory: Memory = ctx.application.bot_data["memory"]
        memory.append(candidate)
        try:
            await query.edit_message_text(f"📌 Запам'ятав: _{candidate}_", parse_mode="Markdown")
        except Exception:
            pass
    else:
        try:
            await query.edit_message_text(f"🚫 Не зберігаю: _{candidate}_", parse_mode="Markdown")
        except Exception:
            pass


async def _cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = ctx.application.bot_data["config"]
    agent: Agent = ctx.application.bot_data["agent"]
    if not _gate(ctx, update):
        return
    await agent.clear(update.effective_chat.id)
    await update.effective_message.reply_text("Thread cleared.")


async def _run_agent_instruction(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, instruction: str
) -> None:
    """Route a canned instruction through the agent (shared by shortcut commands)."""
    config: Config = ctx.application.bot_data["config"]
    agent: Agent = ctx.application.bot_data["agent"]
    if not _gate(ctx, update):
        return
    message = update.effective_message
    await ctx.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        reply = await agent.reply(message.chat_id, instruction)
    except Exception:
        log.exception("agent failed on shortcut command")
        await _react(ctx, message.chat_id, message.message_id, "🤯")
        await message.reply_text("Something went wrong. Check the logs.")
        return
    await message.reply_text(reply)
    await _flush_screenshots(ctx, message.chat_id)
    await _react(ctx, message.chat_id, message.message_id, "👍")


async def _cmd_my(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_agent_instruction(update, ctx, PROMPT_MY)


async def _cmd_free(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_agent_instruction(update, ctx, PROMPT_FREE)


async def _cmd_mail(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_agent_instruction(update, ctx, PROMPT_MAIL)


async def _cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_agent_instruction(update, ctx, PROMPT_TODAY)


async def _cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_agent_instruction(update, ctx, PROMPT_WEEK)


async def _run_brief(app: Application, prompt: str) -> str:
    """Run the agent stateless for the morning brief."""
    agent: Agent = app.bot_data["agent"]
    return await agent.one_shot(prompt)


async def _cmd_morning(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the morning brief on demand."""
    if not _gate(ctx, update):
        return
    message = update.effective_message
    await ctx.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        reply = await _run_brief(ctx.application, BRIEF_PROMPT)
    except Exception:
        log.exception("morning brief failed")
        await _react(ctx, message.chat_id, message.message_id, "🤯")
        await message.reply_text("Не вдалось скласти бриф. Див. логи.")
        return
    await message.reply_text(reply)
    await _react(ctx, message.chat_id, message.message_id, "👍")


# ---- memory --------------------------------------------------------------

async def _cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _gate(ctx, update):
        return
    memory: Memory = ctx.application.bot_data["memory"]
    text = " ".join(ctx.args or []).strip()
    if not text:
        await update.effective_message.reply_text(
            "Використання: `/remember <факт для довгострокової пам'яті>`",
            parse_mode="Markdown",
        )
        return
    line = memory.append(text)
    await update.effective_message.reply_text(f"📌 Запам'ятав:\n{line}")


async def _cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _gate(ctx, update):
        return
    memory: Memory = ctx.application.bot_data["memory"]
    needle = " ".join(ctx.args or []).strip()
    if not needle:
        await update.effective_message.reply_text(
            "Використання: `/forget <підрядок для видалення>`", parse_mode="Markdown"
        )
        return
    n = memory.remove_matching(needle)
    if n == 0:
        await update.effective_message.reply_text(f"Нічого не знайшов з підрядком `{needle}`.", parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(f"🗑 Видалив {n} запис(ів).")


async def _cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _gate(ctx, update):
        return
    memory: Memory = ctx.application.bot_data["memory"]
    facts = memory.facts()
    if not facts:
        await update.effective_message.reply_text("Пам'ять порожня. Додай факт через `/remember ...`", parse_mode="Markdown")
        return
    body = "\n".join(f"• {f}" for f in facts)
    await update.effective_message.reply_text(f"🧠 Пам'ять ({len(facts)} факт(ів)):\n\n{body}")


def _mem_prompt_keyboard(candidate_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("📌 Запам'ятати", callback_data=f"mem:yes:{candidate_id}"),
            InlineKeyboardButton("🚫 Ні", callback_data=f"mem:no:{candidate_id}"),
        ]]
    )


async def _maybe_offer_memory(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, user_message: str, reply: str
) -> None:
    """Async post-reply hook: ask Claude for a memory candidate; if one is
    found, send an inline button so the user can accept it."""
    agent: Agent = ctx.application.bot_data["agent"]
    try:
        candidate = await agent.suggest_memory(user_message, reply)
    except Exception:
        log.exception("suggest_memory failed")
        return
    if not candidate:
        return
    # Stash by a short random id so the callback data stays under Telegram's
    # 64-byte limit. Cache is process-memory only; benign if lost on restart.
    pending: dict = ctx.application.bot_data["mem_pending"]
    cid = f"{int(asyncio.get_running_loop().time() * 1000) % 10_000_000}"
    pending[cid] = candidate
    try:
        await ctx.bot.send_message(
            chat_id,
            f"💡 Помітив, що можна запам'ятати:\n\n_{candidate}_\n\nЗберегти?",
            parse_mode="Markdown",
            reply_markup=_mem_prompt_keyboard(cid),
        )
    except Exception:
        log.exception("failed to send memory offer")
        pending.pop(cid, None)


# ---- admin: user management ------------------------------------------------

async def _cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _gate(ctx, update) or not _is_admin(ctx, update):
        return
    from .db import Db

    db: Db = ctx.application.bot_data["db"]
    rows = db.list_users()
    if not rows:
        await update.effective_message.reply_text("Користувачів немає.")
        return
    lines = ["Дозволені користувачі:"]
    for uid, username, is_admin, _ in rows:
        flag = " 👑" if is_admin else ""
        name = f"@{username}" if username else "?"
        lines.append(f"• {uid} {name}{flag}")
    await update.effective_message.reply_text("\n".join(lines))


async def _cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _gate(ctx, update) or not _is_admin(ctx, update):
        return
    msg = update.effective_message
    args = ctx.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await msg.reply_text("Використання: /grant <user_id> [@username]")
        return
    user_id = int(args[0])
    username = args[1].lstrip("@") if len(args) > 1 else None
    from .db import Db

    db: Db = ctx.application.bot_data["db"]
    db.add_user(user_id, username=username, is_admin=False)
    await msg.reply_text(f"✅ Додано: {user_id}" + (f" (@{username})" if username else ""))


async def _cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _gate(ctx, update) or not _is_admin(ctx, update):
        return
    msg = update.effective_message
    args = ctx.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await msg.reply_text("Використання: /revoke <user_id>")
        return
    user_id = int(args[0])
    if user_id == update.effective_user.id:
        await msg.reply_text("Не можна відкликати себе.")
        return
    from .db import Db

    db: Db = ctx.application.bot_data["db"]
    if db.remove_user(user_id):
        await msg.reply_text(f"🗑 Видалено: {user_id}")
    else:
        await msg.reply_text(f"Користувача {user_id} не знайдено.")


async def _on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = ctx.application.bot_data["config"]
    agent: Agent = ctx.application.bot_data["agent"]
    if not _gate(ctx, update):
        return
    message = update.effective_message
    if message is None or not message.text:
        return

    await ctx.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        reply = await agent.reply(message.chat_id, message.text)
    except Exception:
        log.exception("agent failed")
        await _react(ctx, message.chat_id, message.message_id, "🤯")
        await message.reply_text("Something went wrong. Check the logs.")
        return
    await message.reply_text(reply)
    await _flush_screenshots(ctx, message.chat_id)
    await _react(ctx, message.chat_id, message.message_id, "👍")
    ctx.application.create_task(
        _maybe_offer_memory(ctx, message.chat_id, message.text, reply)
    )


async def _on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = ctx.application.bot_data["agent"]
    if not _gate(ctx, update):
        return
    message = update.effective_message
    if not message or not message.photo:
        return

    # Telegram delivers a list of sizes — biggest is last.
    photo = message.photo[-1]
    caption = (message.caption or "").strip() or "Опиши що зображено на цьому фото."

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    file_path = INBOX_DIR / f"{photo.file_unique_id}.jpg"
    try:
        tg_file = await photo.get_file()
        await tg_file.download_to_drive(file_path)
    except Exception:
        log.exception("photo download failed")
        await message.reply_text("Не вдалось завантажити фото.")
        return

    await ctx.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    prompt = (
        f"Користувач надіслав зображення. Скористайся Read tool щоб подивитись файл "
        f"{file_path} (це підтримує зображення) і дай відповідь українською на: {caption}"
    )
    try:
        reply = await agent.reply(message.chat_id, prompt)
    except Exception:
        log.exception("agent failed on photo")
        await _react(ctx, message.chat_id, message.message_id, "🤯")
        await message.reply_text("Something went wrong. Check the logs.")
        return
    finally:
        try:
            file_path.unlink()
        except OSError:
            pass

    await message.reply_text(reply)
    await _flush_screenshots(ctx, message.chat_id)
    await _react(ctx, message.chat_id, message.message_id, "👍")
    ctx.application.create_task(
        _maybe_offer_memory(ctx, message.chat_id, caption, reply)
    )


async def _on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = ctx.application.bot_data["config"]
    agent: Agent = ctx.application.bot_data["agent"]
    transcriber: Transcriber | None = ctx.application.bot_data.get("transcriber")
    if not _gate(ctx, update):
        return
    message = update.effective_message
    voice = message.voice if message else None
    if voice is None:
        return
    if transcriber is None:
        await message.reply_text("Voice support is not enabled in this build.")
        return

    await ctx.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    tmp_dir = Path(tempfile.gettempdir()) / "assistant-voice"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    audio_path = tmp_dir / f"{voice.file_unique_id}.ogg"
    try:
        file = await voice.get_file()
        await file.download_to_drive(audio_path)
    except Exception:
        log.exception("voice download failed")
        await message.reply_text("Не вдалось завантажити голосове.")
        return

    try:
        transcript = await transcriber.transcribe(audio_path)
    except Exception:
        log.exception("transcription failed")
        await message.reply_text("Не вдалось розпізнати голос.")
        return
    finally:
        try:
            audio_path.unlink()
        except OSError:
            pass

    transcript = transcript.strip()
    if not transcript:
        await message.reply_text("Я не почув мовлення у повідомленні.")
        return

    # Echo the transcript so the user knows what was heard.
    await message.reply_text(f"\U0001F399 {transcript}")

    await ctx.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        reply = await agent.reply(message.chat_id, transcript)
    except Exception:
        log.exception("agent failed on voice")
        await _react(ctx, message.chat_id, message.message_id, "🤯")
        await message.reply_text("Something went wrong. Check the logs.")
        return
    await message.reply_text(reply)
    await _flush_screenshots(ctx, message.chat_id)
    await _react(ctx, message.chat_id, message.message_id, "👍")
    ctx.application.create_task(
        _maybe_offer_memory(ctx, message.chat_id, transcript, reply)
    )


async def _notify_admins(app: Application, text: str) -> None:
    """Push a message to every admin user in the allowlist.

    Attaches an inline 🔐 Re-auth button on messages that mention expired
    credentials so the user can trigger recovery with one tap.
    """
    db: Db = app.bot_data["db"]
    helper: HostHelper = app.bot_data["host_helper"]
    keyboard = None
    if helper.available and ("протермін" in text.lower() or "expired" in text.lower() or "зник" in text.lower()):
        keyboard = _reauth_keyboard()
    for user_id, _username, is_admin, _ in db.list_users():
        if not is_admin:
            continue
        try:
            await app.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
        except Exception:
            log.exception("failed to push admin notification to %s", user_id)


async def _post_init(app: Application) -> None:
    app.bot_data["host_helper"] = HostHelper()
    monitor = CredentialsMonitor(notify=lambda text: _notify_admins(app, text))
    monitor.start()
    app.bot_data["credmon"] = monitor

    agent: Agent = app.bot_data["agent"]
    brief = MorningBrief(
        push=lambda text: _notify_admins(app, text),
        get_agent_reply=lambda prompt: _run_brief(app, prompt),
        get_creds_state=lambda: monitor._state or "healthy",
    )
    brief.start()
    app.bot_data["morning"] = brief
    await app.bot.set_my_commands(
        [
            BotCommand("my", "Мої задачі в роботі"),
            BotCommand("free", "Вільні dev-задачі"),
            BotCommand("mail", "Останні листи"),
            BotCommand("today", "Зустрічі сьогодні"),
            BotCommand("week", "Зустрічі на тиждень"),
            BotCommand("morning", "Ранковий бриф зараз"),
            BotCommand("reset", "Очистити контекст розмови"),
            BotCommand("remember", "Запам'ятати факт про мене"),
            BotCommand("memory", "Показати що бот пам'ятає"),
            BotCommand("forget", "Забути факт за підрядком"),
            BotCommand("login", "Інструкція як оновити Claude auth"),
            BotCommand("reauth", "Запустити claude login на хості"),
            BotCommand("users", "(admin) Список користувачів"),
            BotCommand("grant", "(admin) Додати користувача"),
            BotCommand("revoke", "(admin) Видалити користувача"),
        ]
    )


def build_application(
    config: Config,
    agent: Agent,
    db: Db,
    memory: Memory,
    transcriber: Transcriber | None = None,
) -> Application:
    app = ApplicationBuilder().token(config.telegram_bot_token).post_init(_post_init).build()
    app.bot_data["config"] = config
    app.bot_data["agent"] = agent
    app.bot_data["db"] = db
    app.bot_data["memory"] = memory
    app.bot_data["transcriber"] = transcriber
    # Ephemeral store: message_id → proposed memory candidate awaiting user y/n.
    app.bot_data["mem_pending"] = {}
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("reset", _cmd_reset))
    app.add_handler(CommandHandler("login", _cmd_login))
    app.add_handler(CommandHandler("reauth", _cmd_reauth))
    app.add_handler(CommandHandler("remember", _cmd_remember))
    app.add_handler(CommandHandler("memory", _cmd_memory))
    app.add_handler(CommandHandler("forget", _cmd_forget))
    app.add_handler(CallbackQueryHandler(_on_callback))
    app.add_handler(CommandHandler("my", _cmd_my))
    app.add_handler(CommandHandler("free", _cmd_free))
    app.add_handler(CommandHandler("mail", _cmd_mail))
    app.add_handler(CommandHandler("today", _cmd_today))
    app.add_handler(CommandHandler("week", _cmd_week))
    app.add_handler(CommandHandler("morning", _cmd_morning))
    app.add_handler(CommandHandler("users", _cmd_users))
    app.add_handler(CommandHandler("grant", _cmd_grant))
    app.add_handler(CommandHandler("revoke", _cmd_revoke))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    app.add_handler(MessageHandler(filters.VOICE, _on_voice))
    app.add_handler(MessageHandler(filters.PHOTO, _on_photo))
    return app
