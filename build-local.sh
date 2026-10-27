#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
export BOT_STATS_UPDATER_IMAGE=bot-stats-updater-local
docker build -t "$BOT_STATS_UPDATER_IMAGE" .
docker compose up -d
