#!/bin/sh
# Run the assistant container inside rancher-desktop WSL.
# Usage: wsl -d rancher-desktop -- sh /mnt/d/GitHubProjects/AiAssistent/deploy/run-in-wsl.sh
#
# Idempotent: removes any prior `assistant` container, then starts a fresh one
# with --restart=unless-stopped so it survives container/daemon restarts
# (effectively always-on while Rancher Desktop runs).
set -eu

NAME=assistant
IMAGE=assistant:0.1.0
PROJECT_DIR=/mnt/d/GitHubProjects/AiAssistent
CLAUDE_DIR=/mnt/c/Users/V.Bratskyi/.claude
CLAUDE_JSON=/mnt/c/Users/V.Bratskyi/.claude.json
MODELS_DIR=$PROJECT_DIR/models
ENV_FILE=$PROJECT_DIR/.env

# Stop and remove any previous instance so we boot clean.
docker rm -f "$NAME" >/dev/null 2>&1 || true

# Named volumes default to root ownership on first mount; the bot runs as
# uid 1000 (`assistant`) and can't write the sqlite db. Fix once — chown
# is persisted on the volume.
docker run --rm --user 0:0 -v assistant-data:/data "$IMAGE" \
    chown -R 1000:1000 /data >/dev/null

# Run as the in-image `assistant` user (uid 1000) — the Claude CLI refuses to
# use --dangerously-skip-permissions (our bypassPermissions mode) when uid is
# root, so root is not an option here. Windows files come through 9p with
# permissive perms in Rancher Desktop, so uid 1000 can read/write them.
docker run -d \
    --name "$NAME" \
    --restart=unless-stopped \
    --env-file "$ENV_FILE" \
    -e HOME=/home/assistant \
    -e ASSISTANT_DB_PATH=/data/assistant.sqlite \
    -e WHISPER_MODEL_SIZE=/models \
    -v "$CLAUDE_DIR":/home/assistant/.claude \
    -v "$CLAUDE_JSON":/home/assistant/.claude.json \
    -v "$MODELS_DIR":/models:ro \
    -v /mnt/c:/host/c:ro \
    -v /mnt/d:/host/d:ro \
    -v assistant-data:/data \
    "$IMAGE"

echo
docker ps --filter "name=^${NAME}$" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
echo
echo "Logs: docker logs -f $NAME"
