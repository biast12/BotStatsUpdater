#!/usr/bin/env bash
# ============================================
#   BotStatsUpdater Rebuild Script
#   Rebuilds the image, then recreates containers
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

echo "Rebuilding BotStatsUpdater image and containers..."
docker compose up -d --build --force-recreate --remove-orphans "$@"

echo "Verifying container status..."
docker compose ps

echo "Rebuild completed successfully!"
