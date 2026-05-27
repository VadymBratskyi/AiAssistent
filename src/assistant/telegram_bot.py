from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agent import Agent
from .config import Config
from .graph import GraphAuth, GraphAuthError, GraphClient
from .stt import Transcriber

log = logging.getLogger(__name__)

# Known Atlassian cloud for this user (see project CLAUDE.md).
JIRA_CLOUD_ID = "5989155e-2f13-4fe3-9126-df63c85122a8"

# Persistent tappable keyboard shown under the message input.
# Buttons send literal "/my" etc., which the CommandHandlers pick up.
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["/my", "/free"], ["/reset"]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Команда або повідомлення…",
)

# Canned instructions for the shortcut commands. Phrased as natural requests
# so they sit cleanly in the conversation history and the agent (which has the
# Atlassian MCP) executes them with the exact JQL — no guessing.
PROMPT_MY = (
    "Покажи мої задачі в роботі. Використай Atlassian Jira MCP з "
    f'cloudId {JIRA_CLOUD_ID} і JQL: assignee = currentUser() AND status = "In Progress". '
    "Виведи стисло для Telegram: для кожної задачі — ключ, summary, статус і пряме посилання. "
    "Якщо задач немає — так і скажи одним рядком."
)
PROMPT_FREE = (
    "Покажи вільні dev-задачі. Використай Atlassian Jira MCP з "
    f'cloudId {JIRA_CLOUD_ID} і JQL: status = "Selected for Development" AND labels = Dev. '
    "Виведи стисло для Telegram: для кожної задачі — ключ, summary, статус і пряме посилання. "
    "Якщо задач немає — так і скажи одним рядком."
)


def _gate(config: Config, update: Update) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        return False
    if not config.is_chat_allowed(chat.id):
        log.warning(
            "rejected message from non-allowlisted chat_id=%s username=%s",
            chat.id,
            user.username if user else None,
        )
        return False
    return True


async def _cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = ctx.application.bot_data["config"]
    chat = update.effective_chat
    if chat is None:
        return
    if not config.is_chat_allowed(chat.id):
        log.info("/start from non-allowlisted chat_id=%s — add this id to TELEGRAM_ALLOWED_CHAT_IDS", chat.id)
        await update.effective_message.reply_text(
            f"This bot is single-user. Your chat_id is {chat.id}. "
            "Add it to TELEGRAM_ALLOWED_CHAT_IDS and restart the bot."
        )
        return
    await update.effective_message.reply_text(
        "Привіт, Вадімич. Я на зв'язку.\n\n"
        "Команди (кнопки нижче):\n"
        "/my — мої задачі в роботі\n"
        "/free — вільні dev-задачі (Selected for Development)\n"
        "/reset — очистити контекст розмови\n\n"
        "Або просто пиши/говори природною мовою — Jira вже підключена.",
        reply_markup=MAIN_KEYBOARD,
    )


async def _cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = ctx.application.bot_data["config"]
    agent: Agent = ctx.application.bot_data["agent"]
    if not _gate(config, update):
        return
    agent.clear(update.effective_chat.id)
    await update.effective_message.reply_text("Thread cleared.")


async def _run_agent_instruction(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, instruction: str
) -> None:
    """Route a canned instruction through the agent (shared by shortcut commands)."""
    config: Config = ctx.application.bot_data["config"]
    agent: Agent = ctx.application.bot_data["agent"]
    if not _gate(config, update):
        return
    message = update.effective_message
    await ctx.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        reply = await agent.reply(message.chat_id, instruction)
    except Exception:
        log.exception("agent failed on shortcut command")
        await message.reply_text("Something went wrong. Check the logs.")
        return
    await message.reply_text(reply)


async def _cmd_my(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_agent_instruction(update, ctx, PROMPT_MY)


async def _cmd_free(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_agent_instruction(update, ctx, PROMPT_FREE)


async def _cmd_login_ms(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = ctx.application.bot_data["config"]
    graph_auth: GraphAuth | None = ctx.application.bot_data.get("graph_auth")
    if not _gate(config, update):
        return
    message = update.effective_message
    if graph_auth is None:
        await message.reply_text("MS Graph не налаштований у цьому білді.")
        return
    try:
        flow = graph_auth.start_device_flow()
    except GraphAuthError as e:
        log.warning("device flow start failed: %s", e)
        await message.reply_text(
            "Не вдалося почати вхід. Можливо tenant блокує device-code flow.\n\n"
            f"Деталі: {e}"
        )
        return

    await message.reply_text(
        "🔐 Вхід у Microsoft 365:\n\n"
        f"1. Відкрий {flow.verification_uri}\n"
        f"2. Введи код: {flow.user_code}\n"
        "3. Залогінься робочим акаунтом і підтверди доступ\n\n"
        "Чекаю на завершення… (повідомлю коли готово)"
    )

    chat_id = message.chat_id

    async def _await_completion() -> None:
        try:
            await graph_auth.complete_device_flow(flow)
        except GraphAuthError as e:
            log.warning("device flow completion failed: %s", e)
            await ctx.bot.send_message(
                chat_id,
                "❌ Вхід не вдався. Tenant міг заблокувати ці права (потрібен admin consent).\n\n"
                f"Деталі: {e}",
            )
            return
        await ctx.bot.send_message(chat_id, "✅ Вхід у Microsoft 365 виконано. Тепер працюють пошта і календар.")

    ctx.application.create_task(_await_completion())


async def _cmd_whoami_ms(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = ctx.application.bot_data["config"]
    graph_client: GraphClient | None = ctx.application.bot_data.get("graph_client")
    if not _gate(config, update):
        return
    message = update.effective_message
    if graph_client is None:
        await message.reply_text("MS Graph не налаштований у цьому білді.")
        return
    await ctx.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        me = await graph_client.me()
    except GraphAuthError as e:
        await message.reply_text(f"Не авторизовано або помилка Graph:\n{e}")
        return
    await message.reply_text(
        f"👤 {me.get('displayName')}\n"
        f"✉️ {me.get('mail') or me.get('userPrincipalName')}"
    )


async def _on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = ctx.application.bot_data["config"]
    agent: Agent = ctx.application.bot_data["agent"]
    if not _gate(config, update):
        return
    message = update.effective_message
    if message is None or not message.text:
        return

    await ctx.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        reply = await agent.reply(message.chat_id, message.text)
    except Exception:
        log.exception("agent failed")
        await message.reply_text("Something went wrong. Check the logs.")
        return
    await message.reply_text(reply)


async def _on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = ctx.application.bot_data["config"]
    agent: Agent = ctx.application.bot_data["agent"]
    transcriber: Transcriber | None = ctx.application.bot_data.get("transcriber")
    if not _gate(config, update):
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
        await message.reply_text("Something went wrong. Check the logs.")
        return
    await message.reply_text(reply)


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("my", "Мої задачі в роботі"),
            BotCommand("free", "Вільні dev-задачі"),
            BotCommand("login_ms", "Вхід у Microsoft 365"),
            BotCommand("whoami_ms", "Перевірити вхід M365"),
            BotCommand("reset", "Очистити контекст розмови"),
        ]
    )


def build_application(
    config: Config,
    agent: Agent,
    transcriber: Transcriber | None = None,
    graph_auth: GraphAuth | None = None,
    graph_client: GraphClient | None = None,
) -> Application:
    app = ApplicationBuilder().token(config.telegram_bot_token).post_init(_post_init).build()
    app.bot_data["config"] = config
    app.bot_data["agent"] = agent
    app.bot_data["transcriber"] = transcriber
    app.bot_data["graph_auth"] = graph_auth
    app.bot_data["graph_client"] = graph_client
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("reset", _cmd_reset))
    app.add_handler(CommandHandler("my", _cmd_my))
    app.add_handler(CommandHandler("free", _cmd_free))
    app.add_handler(CommandHandler("login_ms", _cmd_login_ms))
    app.add_handler(CommandHandler("whoami_ms", _cmd_whoami_ms))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    app.add_handler(MessageHandler(filters.VOICE, _on_voice))
    return app
