FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libgomp1: CTranslate2 (faster-whisper backend) on Linux.
# curl, ca-certificates, gnupg: NodeSource APT repo for Node.js.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 curl ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 — for the Playwright MCP server (browser automation).
RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Playwright MCP server + Chromium (with system libs the browser needs).
# Installed into the system Playwright cache so the assistant user finds it.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN npm install -g @playwright/mcp@latest \
    && npx playwright install --with-deps chromium

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN useradd --create-home --shell /bin/bash assistant \
    && mkdir -p /data \
    && chown -R assistant:assistant /data /app
USER assistant

ENV ASSISTANT_DB_PATH=/data/assistant.sqlite

CMD ["python", "-m", "assistant"]
