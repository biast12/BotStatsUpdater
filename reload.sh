#!/usr/bin/env bash
# ============================================
#   BotStatsUpdater Reload Script
#   Recreates containers from existing image (no rebuild)
# ============================================
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker is not installed or not in PATH"
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker daemon is not running. Start it and try again."
    exit 1
fi

echo "Reloading BotStatsUpdater containers..."
docker compose up -d --force-recreate --remove-orphans "$@"

echo "Verifying container status..."
docker compose ps

echo "Reload completed successfully!"
