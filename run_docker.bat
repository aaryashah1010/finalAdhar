@echo off
setlocal

set "APP_URL=http://localhost:6080/vnc.html?autoconnect=true&resize=scale"

cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 (
    echo Docker was not found.
    echo.
    echo Install Docker Desktop, start it, then run this file again.
    echo https://www.docker.com/products/docker-desktop/
    echo.
    pause
    exit /b 1
)

docker info >nul 2>nul
if errorlevel 1 (
    echo Docker is installed but not running.
    echo.
    echo Start Docker Desktop and wait until it says Docker is running.
    echo Then run this file again.
    echo.
    pause
    exit /b 1
)

docker compose version >nul 2>nul
if errorlevel 1 (
    echo Docker Compose was not found.
    echo.
    echo Update Docker Desktop, then run this file again.
    echo.
    pause
    exit /b 1
)

if not exist "client-data\input" mkdir "client-data\input"
if not exist "client-data\output" mkdir "client-data\output"

echo Put PDFs in: %CD%\client-data\input
echo Deleted/moved files will go to: %CD%\client-data\output
echo.
echo Browser URL:
echo %APP_URL%
echo.
echo Starting Docker container. The first run can take several minutes.
echo Keep this window open while using the app.
echo.

start "" "%APP_URL%"
docker compose up --build

echo.
echo Docker app stopped.
pause
