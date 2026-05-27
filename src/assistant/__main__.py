from __future__ import annotations

import logging

from .agent import Agent
from .config import load_config
from .db import Db
from .graph import GraphAuth, GraphClient
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

    if not config.telegram_allowed_chat_ids:
        log.warning(
            "TELEGRAM_ALLOWED_CHAT_IDS is empty — bot will reject every chat. "
            "Send /start to the bot to discover your chat_id from the logs, "
            "then add it and restart."
        )

    db = Db(config.db_path)
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

    graph_auth = GraphAuth(cache_path=config.db_path.parent / "msal_cache.bin")
    graph_client = GraphClient(graph_auth)

    app = build_application(
        config,
        agent,
        transcriber=transcriber,
        graph_auth=graph_auth,
        graph_client=graph_client,
    )

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
