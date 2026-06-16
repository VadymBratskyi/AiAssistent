#!/bin/sh
# Build the assistant image inside the rancher-desktop WSL distro.
# Run as: wsl -d rancher-desktop -- sh /mnt/d/GitHubProjects/AiAssistent/deploy/build-in-wsl.sh
#
# Why: Rancher Desktop on Windows in dockerd mode runs the daemon inside the
# WSL distro. The Windows-side docker CLI doesn't reach it cleanly, and BuildKit
# tries to invoke credential helpers that are glibc binaries unfit for this
# musl-based distro. Building inside the distro with DOCKER_BUILDKIT=0 (legacy
# builder) sidesteps both problems.
set -eu

export PATH="/mnt/c/Program Files/Rancher Desktop/resources/resources/linux/bin:$PATH"

# Isolated docker config — no credsStore, so BuildKit (if anything reaches for
# it) doesn't try to launch a missing helper binary.
export DOCKER_CONFIG=/tmp/assistant-dockercfg
mkdir -p "$DOCKER_CONFIG"
echo '{}' > "$DOCKER_CONFIG/config.json"

cd /mnt/d/GitHubProjects/AiAssistent

# Pre-pull the base image anonymously so the build step never has to.
docker pull python:3.12-slim

# Legacy builder — BuildKit's credential lookup misbehaves in this distro.
DOCKER_BUILDKIT=0 docker build -t assistant:0.1.0 .

echo
docker image inspect assistant:0.1.0 --format '{{.Id}} size={{.Size}}'
