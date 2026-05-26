# Personal AI Assistant

Telegram bot → Claude → your stuff (Jira, Outlook/Teams, local repos), running in your local Rancher.

See [the plan](C:\Users\V.Bratskyi\.claude\plans\i-have-an-interesting-giggly-lynx.md) for the full design and milestones.

## Quickstart (local, M1)

**Prereqs:** `claude` CLI installed and logged in (`claude --version` works). Auth flows through your existing Claude Code session — no Anthropic API key needed.

1. Create a bot via [@BotFather](https://t.me/BotFather) and copy the token.
2. Copy `.env.example` to `.env` and fill in:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_ALLOWED_CHAT_IDS` (leave empty on first run; send `/start` and read your chat_id from the logs, then fill it in and restart).
3. Install and run:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate           # PowerShell: .venv\Scripts\Activate.ps1
   pip install -e .
   python -m assistant
   ```

## Deploy to Rancher (M1)

```bash
docker build -t assistant:0.1.0 .
# Create the secret from your .env values:
kubectl apply -f deploy/secret.example.yaml   # after editing
kubectl apply -f deploy/deployment.yaml
kubectl logs -f deploy/assistant
```

No Service or Ingress is needed — the bot uses Telegram long-polling.

## Milestones

- **M1** — Skeleton bot, SQLite persistence, deployable to Rancher. *(current)*
- **M2** — Jira via Atlassian MCP, Q&A over your board.
- **M3** — Local action catalog, scoped FS reads, Claude Code launcher.
- **M4** — "Analyze ticket → draft PR" workflow.
- **M5** — Outlook + Teams via MS Graph (blocked on IT approval).
