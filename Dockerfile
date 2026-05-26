FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN useradd --create-home --shell /bin/bash assistant \
    && mkdir -p /data \
    && chown -R assistant:assistant /data /app
USER assistant

ENV ASSISTANT_DB_PATH=/data/assistant.sqlite

CMD ["python", "-m", "assistant"]
