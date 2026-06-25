from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
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

Local filesystem access (read-only):
- The host's drives are mounted read-only inside the container:
  - Windows `C:\\...` → `/host/c/...`
  - Windows `D:\\...` → `/host/d/...`
- Use the built-in Read / Glob / Grep / Bash tools against those paths. Translate any Windows path the user mentions (e.g. `D:\\GitHubProjects\\AiAssistent\\README.md`) to its `/host/*` equivalent before reading.
- Read-only mount: writes will fail. Don't try to modify host files.

NEVER read or report contents of files matching these sensitive patterns (refuse politely and explain why):
- Anything under `.ssh/`, `.gnupg/`, `.aws/`, `.azure/`, `.gcloud/`
- `id_rsa`, `id_ed25519`, `id_ecdsa`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.crt` (private material)
- `.env`, `.env.*`, `secrets.*`, `credentials*`, `*.pgpass`, `.netrc`, `.git-credentials`
- Browser profile dirs that store passwords / cookies (`AppData\\Local\\*\\User Data`, `Login Data`, `Cookies`)
- Windows registry hives, `SAM`, `SYSTEM` files
If the user explicitly insists on a specific sensitive file, refuse once and ask them to copy it manually — never read it silently.

How to behave:
- Always actually call the relevant tool to answer. Do not preempt or guess.
- Report what the tool returns. If a tool returns an error, quote the error briefly and stop — do not invent a recovery instruction.
- Empty result is a valid answer; just say "нічого не знайдено" or equivalent.
- Read-only by default. Never send mail, post comments, create/edit tickets, or delete anything unless the user explicitly asked.

Out of scope:
- Drafting PRs from Jira tickets — not implemented yet.
"""


class Agent:
    """Long-lived `claude` subprocess per chat — keeps conversation context
    in-memory across turns without the on-disk resume that caused tool-use
    skipping."""

    def __init__(self, db: Db, model: str | None = None) -> None:
        self.db = db
        self.model = model
        self._clients: dict[int, ClaudeSDKClient] = {}
        # Per-chat lock so two messages from the same chat don't race on the
        # same client (the SDK is not safe for overlapping query() calls).
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            model=self.model,
            max_turns=10,
            permission_mode="bypassPermissions",
        )

    async def _client(self, chat_id: int) -> ClaudeSDKClient:
        client = self._clients.get(chat_id)
        if client is None:
            client = ClaudeSDKClient(options=self._options())
            await client.connect()
            self._clients[chat_id] = client
            log.info("opened claude session chat_id=%s", chat_id)
        return client

    async def _drop_client(self, chat_id: int) -> None:
        client = self._clients.pop(chat_id, None)
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            log.debug("disconnect failed for chat_id=%s", chat_id, exc_info=True)

    async def reply(self, chat_id: int, user_message: str) -> str:
        self.db.append_message(chat_id, "user", user_message)

        async with self._locks[chat_id]:
            try:
                reply_text = await self._send(chat_id, user_message)
            except Exception:
                # Subprocess may be stuck or dead — drop and retry once with
                # a fresh client. Conversation context for that chat is lost.
                log.warning("claude session error chat_id=%s; recreating", chat_id, exc_info=True)
                await self._drop_client(chat_id)
                reply_text = await self._send(chat_id, user_message)

        self.db.append_message(chat_id, "assistant", reply_text)
        return reply_text

    async def _send(self, chat_id: int, user_message: str) -> str:
        client = await self._client(chat_id)
        await client.query(user_message)

        text_parts: list[str] = []
        usage_info = None
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                usage_info = getattr(msg, "usage", None)

        reply_text = "\n".join(text_parts).strip() or "(no response)"
        log.info("claude reply chat_id=%s usage=%s", chat_id, usage_info)
        return reply_text

    async def clear(self, chat_id: int) -> None:
        await self._drop_client(chat_id)
        self.db.clear_thread(chat_id)


__all__ = ["Agent", "SYSTEM_PROMPT"]
