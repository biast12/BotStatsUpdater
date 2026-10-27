@echo off
setlocal
cd /d "%~dp0"
set "BOT_STATS_UPDATER_IMAGE=bot-stats-updater-local"
docker build -t %BOT_STATS_UPDATER_IMAGE% . && docker compose up -d
if errorlevel 1 pause
