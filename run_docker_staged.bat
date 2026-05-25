@echo off
setlocal

set "APP_URL=http://localhost:6080/vnc.html?autoconnect=true^&resize=scale"
set "APP_IMAGE=finaladhar-aadhaar-detector:latest"

cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 (
    echo Docker was not found.
    echo Install Docker Desktop, start it, then run this file again.
    pause
    exit /b 1
)

docker info >nul 2>nul
if errorlevel 1 (
    echo Docker is installed but not running.
    echo Start Docker Desktop and wait until it says Docker is running.
    pause
    exit /b 1
)

docker compose version >nul 2>nul
if errorlevel 1 (
    echo Docker Compose was not found.
    echo Update Docker Desktop, then run this file again.
    pause
    exit /b 1
)

set "WORK_ROOT=%CD%\client-data\staged-run"
set "LOCAL_INPUT=%WORK_ROOT%\input"
set "LOCAL_OUTPUT=%WORK_ROOT%\deleted"

if not exist "%WORK_ROOT%" mkdir "%WORK_ROOT%"
if not exist "%LOCAL_OUTPUT%" mkdir "%LOCAL_OUTPUT%"

echo Select the SHARED parent folder that contains PDFs and subfolders.
echo Example: \\SERVER\Share\ParentFolder
for /f "usebackq delims=" %%I in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; $f=New-Object System.Windows.Forms.FolderBrowserDialog; $f.Description='Select SHARED parent folder containing PDFs'; if ($f.ShowDialog() -eq 'OK') { $f.SelectedPath }"`) do set "SHARED_INPUT=%%I"

if "%SHARED_INPUT%"=="" (
    echo No shared input folder selected.
    pause
    exit /b 1
)

echo.
echo Local input folder:
echo %LOCAL_INPUT%
echo.
echo Local deleted/output folder:
echo %LOCAL_OUTPUT%
echo.
echo This will clear the local input folder and copy all PDFs from:
echo %SHARED_INPUT%
echo.
choice /C YN /M "Continue"
if errorlevel 2 exit /b 1

if exist "%LOCAL_INPUT%" rmdir /s /q "%LOCAL_INPUT%"
mkdir "%LOCAL_INPUT%"

echo.
echo Copying PDFs from shared folder to local input...
robocopy "%SHARED_INPUT%" "%LOCAL_INPUT%" *.pdf /E /R:2 /W:2
set "ROBOCOPY_EXIT=%ERRORLEVEL%"

if %ROBOCOPY_EXIT% GEQ 8 (
    echo.
    echo Robocopy failed with exit code %ROBOCOPY_EXIT%.
    echo Check network access and selected folder permissions.
    pause
    exit /b %ROBOCOPY_EXIT%
)

set "AADHAAR_HOST_INPUT=%LOCAL_INPUT%"
set "AADHAAR_HOST_OUTPUT=%LOCAL_OUTPUT%"

echo.
echo Copy complete.
echo Starting Aadhaar detector using local folders only.
echo.
echo Input:  %AADHAAR_HOST_INPUT%
echo Output: %AADHAAR_HOST_OUTPUT%
echo.
echo Browser URL:
echo http://localhost:6080/vnc.html?autoconnect=true^&resize=scale
echo.

start "" "%APP_URL%"
docker image inspect "%APP_IMAGE%" >nul 2>nul
if errorlevel 1 (
    echo Docker image not found locally. Building it now.
    docker compose build
    if errorlevel 1 (
        echo Docker build failed.
        pause
        exit /b 1
    )
) else (
    echo Docker image found locally. Skipping build.
)

docker compose up

echo.
echo Docker app stopped.
echo Deleted Aadhaar files are in:
echo %LOCAL_OUTPUT%
pause
