from __future__ import annotations

import logging
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from .db import Db

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a personal AI assistant talking to your principal user over Telegram.

Style:
- Concise. Telegram on a phone. Short paragraphs, light markdown.
- Reply in the language the user wrote in.
- Times for this user are Kyiv (UTC+3) unless stated otherwise.

Connectors available to you via MCP:
- Atlassian (Jira + Confluence) on creatio-misk.atlassian.net, cloudId 5989155e-2f13-4fe3-9126-df63c85122a8.
- Microsoft 365 (Outlook mail, Teams meetings / calendar, SharePoint).

How to behave:
- Always actually call the relevant tool to answer. Do not preempt or guess.
- Report what the tool returns. If a tool returns an error, quote the error briefly and stop — do not invent a recovery instruction.
- Empty result is a valid answer; just say "нічого не знайдено" or equivalent.
- Read-only by default. Never send mail, post comments, create/edit tickets, or delete anything unless the user explicitly asked.

Out of scope:
- Drafting PRs from Jira tickets — not implemented yet.
"""


@dataclass
class Agent:
    db: Db
    model: str | None = None  # None = SDK default

    async def reply(self, chat_id: int, user_message: str) -> str:
        self.db.append_message(chat_id, "user", user_message)
        prior_session = self.db.get_session_id(chat_id)

        options = ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            # Resume disabled: resuming a prior session was making Claude
            # skip MCP tool calls and reply with hallucinated "disconnected"
            # text. Each turn now starts fresh — short-term memory inside a
            # turn still works; cross-turn memory is gone, which is fine for
            # the bot's usage (mostly one-shot commands).
            model=self.model,
            max_turns=10,
            permission_mode="bypassPermissions",
        )
        _ = prior_session  # kept for future debug; unused while resume is off

        reply_parts: list[str] = []
        new_session_id: str | None = None
        usage_info: dict | None = None

        try:
            async for msg in query(prompt=user_message, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            reply_parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    new_session_id = getattr(msg, "session_id", None)
                    usage_info = getattr(msg, "usage", None)
        except Exception:
            log.exception("Claude Agent SDK query failed (chat_id=%s)", chat_id)
            # If session resume was the culprit, drop it so next call starts fresh.
            if prior_session:
                self.db.set_session_id(chat_id, "")
            raise

        reply_text = "\n".join(reply_parts).strip() or "(no response)"

        if new_session_id:
            self.db.set_session_id(chat_id, new_session_id)
        self.db.append_message(chat_id, "assistant", reply_text)

        log.info(
            "claude reply chat_id=%s resumed=%s new_session=%s usage=%s",
            chat_id,
            bool(prior_session),
            bool(new_session_id),
            usage_info,
        )
        return reply_text

    def clear(self, chat_id: int) -> None:
        self.db.clear_thread(chat_id)


__all__ = ["Agent", "SYSTEM_PROMPT"]
