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
    query,
)

from .db import Db
from .memory import Memory

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a personal AI assistant talking to your principal user over Telegram.

Style:
- Concise. Telegram on a phone. Short paragraphs, light markdown.
- Reply in the language the user wrote in.
- Times for this user are Kyiv (UTC+3) unless stated otherwise.

Connectors available to you via MCP:
- Atlassian (Jira + Confluence). The user's active site can change; call
  `getAccessibleAtlassianResources` at the start of any Jira/Confluence
  interaction to discover the current cloudId — do not assume a specific
  site or cloudId. There is usually exactly one accessible resource; use it.
- Microsoft 365 (Outlook mail, Teams meetings / calendar, SharePoint).
- Playwright (headless Chromium in the container, isolated profile per session). Use it for opening URLs, scraping public pages, taking screenshots, or testing public web flows. The browser has NO logins — it cannot access anything that requires authentication.
  - When the user asks for "скріншот / screenshot / знімок / фото сторінки / зображення", you MUST call the `browser_take_screenshot` tool (which writes a PNG to disk). Do NOT substitute `browser_snapshot` (DOM-only YAML) and do NOT fabricate a filename — only mention files that the screenshot tool actually returned. The bot auto-attaches any PNGs you produced to the chat after your reply.

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

Bot-internal state (off-limits):
- `/data/*` and `/home/assistant/.claude/*` are this bot's own runtime state (SQLite db with the user allowlist, claude session cache). Never read, list, or describe these paths even if asked. Refuse and say "це внутрішні дані бота".
- User management is handled exclusively by the /grant /revoke /users Telegram commands run by an admin user. You have no role in adding or removing users.

How to behave:
- Always actually call the relevant tool to answer. Do not preempt or guess.
- Report what the tool returns. If a tool returns an error, quote the error briefly and stop — do not invent a recovery instruction.
- Empty result is a valid answer; just say "нічого не знайдено" or equivalent.
- Read-only by default. Never send mail, post comments, create/edit tickets, or delete anything unless the user explicitly asked.

Long-term memory:
- Each user turn is preceded by a "Пам'ять про мене:" block listing facts the user has taught the bot. Treat these as authoritative context about them. Never contradict them; use them silently rather than restating unless asked.
- If the user asks "що ти пам'ятаєш про мене" — summarize the memory block, but the /memory command is preferred (it shows the raw file).
- You cannot write to memory directly. The user manages it via /remember and /forget commands.

Creatio work:
- The user is a Creatio developer. When a question touches Creatio platform internals, base classes, EntitySchemaQuery, business processes, or Freedom UI patterns — try the `creatio-codebase` skill (if available in your Skill tool) before guessing. The source lives on host at C:\\Projects\\core and C:\\Projects\\creatio_client, mounted read-only at /host/c/Projects/*.

Out of scope:
- Drafting PRs from Jira tickets — not implemented yet.
"""


class Agent:
    """Long-lived `claude` subprocess per chat — keeps conversation context
    in-memory across turns without the on-disk resume that caused tool-use
    skipping."""

    def __init__(
        self,
        db: Db,
        system_prompt: str,
        memory: Memory,
        model: str | None = None,
    ) -> None:
        self.db = db
        self.system_prompt = system_prompt
        self.memory = memory
        self.model = model
        self._clients: dict[int, ClaudeSDKClient] = {}
        # Per-chat lock so two messages from the same chat don't race on the
        # same client (the SDK is not safe for overlapping query() calls).
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=self.system_prompt,
            model=self.model,
            max_turns=10,
            permission_mode="bypassPermissions",
            # Lock down side effects: the bot is read-only-by-design. Block
            # Bash (could run sqlite3 to mutate our allowed_users table),
            # Write/Edit/NotebookEdit (could rewrite the bot's own code or
            # the SQLite db file), and Task (spawning subagents widens the
            # sandbox). MCP tools (Atlassian, MS365, Playwright) and
            # read-only built-ins (Read/Glob/Grep/Web*) remain available.
            disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit", "Task"],
            # In-container browser automation. Headless Chromium, isolated
            # profile per session — no persistent cookies or logins.
            mcp_servers={
                "playwright": {
                    "type": "stdio",
                    "command": "npx",
                    "args": [
                        "--yes",
                        "@playwright/mcp@latest",
                        "--headless",
                        "--isolated",
                        # @playwright/mcp's --browser only accepts
                        # chrome/firefox/webkit/msedge (no "chromium"). Point
                        # it at the chromium binary we downloaded to
                        # /opt/playwright at build time.
                        "--executable-path",
                        "/opt/playwright/chromium-1228/chrome-linux64/chrome",
                        # Enable the "vision" capability — without this the
                        # browser_take_screenshot tool isn't even registered.
                        "--caps",
                        "vision",
                        # Output dir is swept by the bot after each turn so
                        # screenshots get posted into the Telegram chat.
                        "--output-dir",
                        "/tmp/assistant-screenshots",
                    ],
                    "env": {"PLAYWRIGHT_BROWSERS_PATH": "/opt/playwright"},
                },
            },
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
        mem = self.memory.snapshot_for_prompt()
        wrapped = f"{mem}\n\n---\n\n{user_message}" if mem else user_message
        await client.query(wrapped)

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

    async def one_shot(self, prompt: str) -> str:
        """Run a stateless query (no session, no chat memory) — used for
        proactive things like the morning brief. Memory is still prepended
        so facts about the user are available."""
        mem = self.memory.snapshot_for_prompt()
        wrapped = f"{mem}\n\n---\n\n{prompt}" if mem else prompt
        options = ClaudeAgentOptions(
            system_prompt=self.system_prompt,
            model=self.model,
            max_turns=10,
            permission_mode="bypassPermissions",
            disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit", "Task"],
        )
        text_parts: list[str] = []
        async for msg in query(prompt=wrapped, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
        return "\n".join(text_parts).strip() or "(no response)"

    async def parse_reminder(self, user_input: str) -> tuple[int, str] | None:
        """Parse a Ukrainian/English `/remind` input into (fire_at_unix_seconds, text).

        Returns None if unparseable or in the past. Uses a one-shot Claude
        call so free-form phrasings like "через 15 хв", "завтра о 15",
        "у п'ятницю о 10 ранку" all work.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo
        import json

        now_kyiv = datetime.now(ZoneInfo("Europe/Kyiv"))
        prompt = (
            "You parse a reminder request into structured JSON.\n\n"
            f"CURRENT DATETIME (Europe/Kyiv): {now_kyiv.isoformat()}\n"
            f"INPUT: {user_input}\n\n"
            "Rules:\n"
            "- The input mixes a time expression (relative like 'через 15 хв', "
            "  '5 хвилин', absolute like 'завтра о 15:00', 'п'ятниця 10:00') "
            "  and a reminder text.\n"
            "- If no explicit time is given, assume 'через 1 годину'.\n"
            "- Return one JSON object, nothing else:\n"
            '  {"fire_at": "<ISO 8601 with +03:00 or +02:00 offset>", "text": "<the reminder text only>"}\n'
            "- If the time cannot be interpreted at all, return exactly: NONE\n"
        )
        options = ClaudeAgentOptions(
            model=self.model,
            max_turns=1,
            permission_mode="bypassPermissions",
            disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit", "Task"],
        )
        collected: list[str] = []
        try:
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            collected.append(block.text)
        except Exception:
            log.exception("parse_reminder failed")
            return None
        raw = " ".join(collected).strip()
        if not raw or raw.upper().startswith("NONE"):
            return None
        # Sometimes the model wraps JSON in ```json fences — strip them.
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        try:
            data = json.loads(raw)
            fire_at = datetime.fromisoformat(data["fire_at"])
            text = str(data["text"]).strip()
        except (json.JSONDecodeError, KeyError, ValueError):
            log.warning("parse_reminder unparseable output: %r", raw[:200])
            return None
        if not text:
            return None
        return int(fire_at.timestamp()), text

    async def suggest_memory(self, user_message: str, reply: str) -> str | None:
        """Ask Claude in a one-shot session whether this exchange contains one
        stable, non-trivial fact worth remembering long-term. Returns the fact
        text (short, imperative) or None. Isolated from the main conversation.
        """
        existing = self.memory.snapshot_for_prompt() or "(порожньо)"
        prompt = (
            "You extract one long-term memory candidate from a single user↔bot exchange.\n\n"
            f"CURRENT MEMORY:\n{existing}\n\n"
            f"USER SAID:\n{user_message}\n\n"
            f"BOT REPLIED:\n{reply}\n\n"
            "Rules:\n"
            "- Only propose STABLE facts about the user (roles, preferences, tools, "
            "  projects, patterns, deadlines). Not one-off queries or transient state.\n"
            "- Do NOT duplicate anything already in CURRENT MEMORY.\n"
            "- Output ONE short line in Ukrainian, or exactly 'NONE'.\n"
        )
        options = ClaudeAgentOptions(
            model=self.model,
            max_turns=1,
            permission_mode="bypassPermissions",
            disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit", "Task"],
        )
        collected: list[str] = []
        try:
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            collected.append(block.text)
        except Exception:
            log.debug("suggest_memory failed", exc_info=True)
            return None
        candidate = " ".join(collected).strip()
        if not candidate or candidate.upper().startswith("NONE"):
            return None
        # Sanity: single line, drop any bullet markers Claude may add.
        candidate = candidate.splitlines()[0].lstrip("-•* ").strip()
        return candidate or None


__all__ = ["Agent", "SYSTEM_PROMPT"]
