@echo off
setlocal

set "APP_URL=http://localhost:6080/vnc.html?autoconnect=true^&resize=scale"

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

set "DEFAULT_INPUT=%CD%\client-data\input"
set "DEFAULT_OUTPUT=%CD%\client-data\output"

echo Select input folder containing PDFs.
echo Close the picker or press Cancel to use: %DEFAULT_INPUT%
for /f "usebackq delims=" %%I in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; $d='%DEFAULT_INPUT%'; $f=New-Object System.Windows.Forms.FolderBrowserDialog; $f.Description='Select INPUT folder containing PDFs'; $f.SelectedPath=$d; if ($f.ShowDialog() -eq 'OK') { $f.SelectedPath } else { $d }"`) do set "AADHAAR_HOST_INPUT=%%I"

echo Select output folder for deleted/moved files.
echo Close the picker or press Cancel to use: %DEFAULT_OUTPUT%
for /f "usebackq delims=" %%O in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; $d='%DEFAULT_OUTPUT%'; $f=New-Object System.Windows.Forms.FolderBrowserDialog; $f.Description='Select OUTPUT folder for deleted/moved files'; $f.SelectedPath=$d; if ($f.ShowDialog() -eq 'OK') { $f.SelectedPath } else { $d }"`) do set "AADHAAR_HOST_OUTPUT=%%O"

if not exist "%AADHAAR_HOST_INPUT%" mkdir "%AADHAAR_HOST_INPUT%"
if not exist "%AADHAAR_HOST_OUTPUT%" mkdir "%AADHAAR_HOST_OUTPUT%"

echo Put PDFs in: %AADHAAR_HOST_INPUT%
echo Deleted/moved files will go to: %AADHAAR_HOST_OUTPUT%
echo.
echo Browser URL:
echo http://localhost:6080/vnc.html?autoconnect=true^&resize=scale
echo.
echo Starting Docker container. The first run can take several minutes.
echo Keep this window open while using the app.
echo.

start "" "%APP_URL%"
docker compose build
if errorlevel 1 (
    echo.
    echo Docker build failed.
    pause
    exit /b 1
)

docker compose up

echo.
echo Docker app stopped.
pause
