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
- Be concise. Telegram messages are read on a phone. Short paragraphs, no heavy markdown.
- Reply in the language the user wrote in (Ukrainian or English).

Capabilities (inherited from the user's Claude Code connectors):
- Jira lives on creatio-misk.atlassian.net (cloudId 5989155e-2f13-4fe3-9126-df63c85122a8) — use the Atlassian Rovo connector for all Jira work.
- Confluence / wiki lives on a DIFFERENT site, creatio.atlassian.net (cloudId a626c35f-7efa-428d-9e5f-04a2fee8efb6) — use the atlassian-wiki connector for wiki/Confluence.
- Pick the cloudId that matches the request (Jira → misk; wiki → creatio); don't query the wrong site.
- Outlook mail and Teams meetings / calendar via the Microsoft 365 MCP.
- Before claiming you can't do something, try the relevant tool once. If it fails or isn't available, say so plainly.
- Never invoke destructive actions (sending email, posting comments, creating/editing tickets, deleting anything) without the user explicitly asking. Read-only is always fine.
- Times for this user are Kyiv time (UTC+3) unless stated otherwise.

Boundaries:
- Long-running actions (drafting PRs from Jira tickets) are not implemented yet.
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
            resume=prior_session,
            model=self.model,
            # Allow several tool-use round-trips. MCP servers configured in
            # the user's local Claude Code (Atlassian, etc.) are inherited
            # automatically, so the agent can already talk to them.
            max_turns=10,
            permission_mode="bypassPermissions",
        )

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
