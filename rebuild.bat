@echo off
setlocal EnableDelayedExpansion

REM ============================================
REM   BotStatsUpdater Rebuild Script
REM   Rebuilds the image, then recreates containers
REM ============================================

title BotStatsUpdater - Rebuilding...

REM Configuration
set "DOCKER_PATH=C:\Program Files\Docker\Docker\Docker Desktop.exe"
set "DOCKER_TIMEOUT=60"

REM Check Docker installation
title BotStatsUpdater - Checking Docker...
where docker >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker is not installed or not in PATH
    goto :error_exit
)

REM Check if Docker is running
echo Checking Docker status...
docker info >nul 2>&1
if errorlevel 1 (
    title BotStatsUpdater - Starting Docker Desktop...
    echo Docker is not running. Attempting to start Docker Desktop...

    if not exist "%DOCKER_PATH%" (
        echo ERROR: Docker Desktop not found at: %DOCKER_PATH%
        echo Please update the DOCKER_PATH variable in this script.
        goto :error_exit
    )

    start "" "%DOCKER_PATH%"

    set /A counter=0
    :wait_docker
    docker info >nul 2>&1
    if !errorlevel!==0 (
        title BotStatsUpdater - Docker Ready
        echo Docker is now running.
    ) else (
        set /A counter+=1
        if !counter! GEQ %DOCKER_TIMEOUT% (
            echo ERROR: Docker did not start within %DOCKER_TIMEOUT% seconds.
            goto :error_exit
        )
        title BotStatsUpdater - Waiting for Docker... [!counter!/%DOCKER_TIMEOUT%]
        echo Waiting for Docker to start... [!counter!/%DOCKER_TIMEOUT%]
        timeout /t 1 >nul
        goto :wait_docker
    )
) else (
    echo Docker is already running.
)

REM Rebuild image and recreate containers
echo Rebuilding BotStatsUpdater image and containers...
title BotStatsUpdater - Building Image...

docker compose up -d --build --force-recreate --remove-orphans %*
if errorlevel 1 (
    echo ERROR: Docker Compose failed to rebuild containers
    goto :error_exit
)

title BotStatsUpdater - Verifying Containers...
echo Verifying container status...
docker compose ps

title BotStatsUpdater - Rebuild Complete!
echo Rebuild completed successfully!
goto :end

:error_exit
title BotStatsUpdater - Rebuild Failed!
echo Rebuild failed.
echo Press any key to exit...
pause >nul
exit /b 1

:end
endlocal
exit /b 0
