from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .teams import TeamConfig, load_teams


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_allowed_chat_ids: frozenset[int]
    anthropic_model: str | None
    db_path: Path
    log_level: str
    whisper_model_size: str
    whisper_language: str | None
    whisper_cache_dir: Path | None
    auto_pull_repos: tuple[str, ...]
    teams: dict[str, TeamConfig]
    orchestrator_token: str | None
    orchestrator_host: str
    orchestrator_port: int

    def is_chat_allowed(self, chat_id: int) -> bool:
        # Empty allowlist = bootstrap mode: log chat_id so the user can add it,
        # but reject everything. This makes the chat_id discoverable without
        # accidentally leaving the bot open.
        return chat_id in self.telegram_allowed_chat_ids


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _parse_chat_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as e:
            raise RuntimeError(f"Invalid chat id in TELEGRAM_ALLOWED_CHAT_IDS: {part!r}") from e
    return frozenset(ids)


def load_config() -> Config:
    load_dotenv()
    model = os.environ.get("ANTHROPIC_MODEL", "").strip() or None
    whisper_lang = os.environ.get("WHISPER_LANGUAGE", "").strip() or None
    whisper_cache_raw = os.environ.get("WHISPER_CACHE_DIR", "").strip()
    auto_pull_raw = os.environ.get("AUTO_PULL_REPOS", "creatio-ui,creatio-core").strip()
    auto_pull = tuple(r.strip() for r in auto_pull_raw.split(",") if r.strip())
    teams_path = Path(os.environ.get("TEAMS_CONFIG_PATH", "./teams.yaml"))
    teams = load_teams(teams_path)
    orchestrator_token = os.environ.get("ORCHESTRATOR_TOKEN", "").strip() or None
    orchestrator_host = os.environ.get("ORCHESTRATOR_API_HOST", "127.0.0.1").strip()
    orchestrator_port = int(os.environ.get("ORCHESTRATOR_API_PORT", "47824"))
    return Config(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_chat_ids=_parse_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
        anthropic_model=model,
        db_path=Path(os.environ.get("ASSISTANT_DB_PATH", "./assistant.sqlite")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        whisper_model_size=os.environ.get("WHISPER_MODEL_SIZE", "small"),
        whisper_language=whisper_lang,
        whisper_cache_dir=Path(whisper_cache_raw) if whisper_cache_raw else None,
        auto_pull_repos=auto_pull,
        teams=teams,
        orchestrator_token=orchestrator_token,
        orchestrator_host=orchestrator_host,
        orchestrator_port=orchestrator_port,
    )
