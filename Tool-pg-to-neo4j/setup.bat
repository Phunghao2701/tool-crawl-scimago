@echo off
REM Quick setup script for Research Graph Sync (Windows)

echo.
echo ======================================
echo Research Graph Sync Setup
echo ======================================
echo.

REM Check if Docker is installed
where docker >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Docker is not installed or not in PATH
    echo Please install Docker Desktop from https://www.docker.com/products/docker-desktop
    pause
    exit /b 1
)

echo [OK] Docker found

REM Check if Docker Compose is installed
where docker-compose >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Docker Compose is not installed
    pause
    exit /b 1
)

echo [OK] Docker Compose found

REM Create .env if not exists
if not exist .env (
    echo [INFO] Creating .env from template...
    copy .env.example .env
    echo [WARNING] Please edit .env with your settings
)

REM Create logs directory
if not exist logs mkdir logs

REM Pull latest images
echo [INFO] Pulling Docker images...
docker-compose --env-file .env -f docker/docker-compose.yml pull

REM Start services
echo [INFO] Starting services...
docker-compose --env-file .env -f docker/docker-compose.yml up -d

REM Wait for services
echo [INFO] Waiting for services to be healthy (30-60 seconds)...
timeout /t 30 /nobreak

REM Check health
echo [INFO] Checking service health...
docker-compose --env-file .env -f docker/docker-compose.yml ps

echo.
echo ======================================
echo Setup complete!
echo ======================================
echo.
echo Access points:
echo   - Neo4j: http://localhost:7474
echo   - PostgreSQL: localhost:5432
echo.
echo Next steps:
echo   1. Edit .env with your settings if needed
echo   2. Run sync: docker exec -it research_graph_sync python src/main.py --type full
echo.
pause
