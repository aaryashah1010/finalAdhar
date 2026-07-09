@echo off
setlocal

set "APP_URL=http://127.0.0.1:6080/vnc.html?resize=scale"
set "APP_IMAGE=finaladhar-aadhaar-detector:latest"

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
echo http://127.0.0.1:6080/vnc.html?resize=scale
echo.
echo Starting Docker container. The first run can take several minutes.
echo Keep this window open while using the app.
echo.

start "" "%APP_URL%"
docker image inspect "%APP_IMAGE%" >nul 2>nul
if errorlevel 1 (
    echo Docker image not found locally. Building it now.
    docker compose build
    if errorlevel 1 (
        echo.
        echo Docker build failed.
        echo.
        echo If this PC cannot download Debian packages, build the image on another PC:
        echo   docker compose build
        echo   docker save %APP_IMAGE% -o finaladhar-aadhaar-detector.tar
        echo Then copy the TAR here and load it:
        echo   docker load -i finaladhar-aadhaar-detector.tar
        pause
        exit /b 1
    )
) else (
    echo Docker image found locally. Skipping build.
)

docker compose up --force-recreate

echo.
echo Docker app stopped.
pause
