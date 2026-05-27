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
for /f "usebackq delims=" %%T in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Date -Format yyyyMMdd_HHmmss"`) do set "RUN_ID=%%T"
set "LOCAL_INPUT=%WORK_ROOT%\input"
set "LOCAL_OUTPUT=%WORK_ROOT%\deleted\%RUN_ID%"
set "REPORT_DIR=%WORK_ROOT%\reports"
set "SYNC_SCRIPT=%CD%\tools\sync_deleted_to_shared.ps1"

if not exist "%WORK_ROOT%" mkdir "%WORK_ROOT%"
if not exist "%LOCAL_OUTPUT%" mkdir "%LOCAL_OUTPUT%"
if not exist "%REPORT_DIR%" mkdir "%REPORT_DIR%"

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

echo.
choice /C YN /M "Sync deleted/masked files back to shared drive"
if errorlevel 2 goto end

if not exist "%SYNC_SCRIPT%" (
    echo Sync script not found:
    echo %SYNC_SCRIPT%
    echo Cannot sync back to shared folders.
    goto end
)

echo.
echo Select the SHARED output folder where deleted Aadhaar files should be copied.
echo Example: \\SERVER\Share\AadhaarDeleted
for /f "usebackq delims=" %%O in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; $f=New-Object System.Windows.Forms.FolderBrowserDialog; $f.Description='Select SHARED output folder for deleted Aadhaar files'; if ($f.ShowDialog() -eq 'OK') { $f.SelectedPath }"`) do set "SHARED_OUTPUT=%%O"

if "%SHARED_OUTPUT%"=="" (
    echo No shared output folder selected. Skipping sync.
    goto end
)

echo.
echo This will:
echo   1. Copy deleted Aadhaar files to: %SHARED_OUTPUT%
echo   2. Remove matching originals from: %SHARED_INPUT%
echo   3. Replace masked-and-kept originals in: %SHARED_INPUT%
echo.
echo Shared files are changed only after copy verification.
choice /C YN /M "Proceed with shared-drive removal"
if errorlevel 2 goto end

powershell -NoProfile -ExecutionPolicy Bypass -File "%SYNC_SCRIPT%" -SharedInput "%SHARED_INPUT%" -LocalDeleted "%LOCAL_OUTPUT%" -SharedOutput "%SHARED_OUTPUT%" -LocalInput "%LOCAL_INPUT%" -ReportDir "%REPORT_DIR%"
if errorlevel 1 (
    echo.
    echo Sync finished with errors. Check the report in:
    echo %REPORT_DIR%
    goto end
)

echo.
echo Sync completed successfully. Report is in:
echo %REPORT_DIR%

:end
pause
