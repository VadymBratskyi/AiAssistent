from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from telegram import Update
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
from .stt import Transcriber

log = logging.getLogger(__name__)


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
        "Hi. I'm online. Ask me anything — Jira / Outlook / local actions land in later milestones."
    )


async def _cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = ctx.application.bot_data["config"]
    agent: Agent = ctx.application.bot_data["agent"]
    if not _gate(config, update):
        return
    agent.clear(update.effective_chat.id)
    await update.effective_message.reply_text("Thread cleared.")


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


def build_application(
    config: Config,
    agent: Agent,
    transcriber: Transcriber | None = None,
) -> Application:
    app = ApplicationBuilder().token(config.telegram_bot_token).build()
    app.bot_data["config"] = config
    app.bot_data["agent"] = agent
    app.bot_data["transcriber"] = transcriber
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("reset", _cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    app.add_handler(MessageHandler(filters.VOICE, _on_voice))
    return app
