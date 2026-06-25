from __future__ import annotations

import logging

from .agent import Agent
from .config import load_config
from .db import Db
from .sslfix import sanitize_ca_env
from .stt import Transcriber
from .telegram_bot import build_application


def main() -> None:
    sanitize_ca_env()
    config = load_config()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("assistant")

    db = Db(config.db_path)

    # One-time bootstrap: if the DB user allowlist is empty, seed it from the
    # env allowlist as admins. From this point on, DB is the source of truth
    # and admins can /grant /revoke through Telegram without restarts.
    if db.count_users() == 0:
        if config.telegram_allowed_chat_ids:
            for uid in config.telegram_allowed_chat_ids:
                db.add_user(uid, is_admin=True)
            log.info(
                "seeded user allowlist from env with %d admin(s)",
                len(config.telegram_allowed_chat_ids),
            )
        else:
            log.warning(
                "user allowlist is empty and no env seed — bot will reject every chat. "
                "Send /start to discover your user_id, then add it manually via SQL "
                "or restart with TELEGRAM_ALLOWED_CHAT_IDS set."
            )
    agent = Agent(db=db, model=config.anthropic_model)

    transcriber = None
    try:
        transcriber = Transcriber(
            model_size=config.whisper_model_size,
            language=config.whisper_language,
            download_root=config.whisper_cache_dir,
        )
    except Exception:
        log.exception(
            "failed to initialize whisper transcriber — voice messages will be disabled. "
            "Bot will continue serving text."
        )

    app = build_application(config, agent, db, transcriber=transcriber)

    log.info(
        "assistant starting; db=%s model=%s whisper=%s lang=%s",
        config.db_path,
        config.anthropic_model or "<sdk default>",
        config.whisper_model_size,
        config.whisper_language or "auto",
    )
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
