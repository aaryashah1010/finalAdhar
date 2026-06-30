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

set "STAGED_BASE=%CD%\client-data\staged-run"
set "SYNC_SCRIPT=%CD%\tools\sync_deleted_to_shared.ps1"
for /f "usebackq delims=" %%T in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Date -Format yyyyMMdd_HHmmss"`) do set "RUN_ID=%%T"

echo Select the SHARED parent folder that contains PDFs and subfolders.
echo Example: \\SERVER\Share\ParentFolder
for /f "usebackq delims=" %%I in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; $f=New-Object System.Windows.Forms.FolderBrowserDialog; $f.Description='Select SHARED parent folder containing PDFs'; if ($f.ShowDialog() -eq 'OK') { $f.SelectedPath }"`) do set "SHARED_INPUT=%%I"

if "%SHARED_INPUT%"=="" (
    echo No shared input folder selected.
    pause
    exit /b 1
)

echo.
echo Select the SHARED output folder where deleted Aadhaar files should be copied.
echo Example: \\SERVER\Share\AadhaarDeleted
for /f "usebackq delims=" %%O in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; $f=New-Object System.Windows.Forms.FolderBrowserDialog; $f.Description='Select SHARED output folder for deleted Aadhaar files'; if ($f.ShowDialog() -eq 'OK') { $f.SelectedPath }"`) do set "SHARED_OUTPUT=%%O"

if "%SHARED_OUTPUT%"=="" (
    echo No shared output folder selected.
    pause
    exit /b 1
)

for %%P in ("%SHARED_INPUT%") do set "RUN_KEY=%%~nxP"
if "%RUN_KEY%"=="" set "RUN_KEY=default"

set "WORK_ROOT=%STAGED_BASE%\%RUN_KEY%"
set "LOCAL_INPUT=%WORK_ROOT%\input"
set "LOCAL_OUTPUT=%WORK_ROOT%\deleted"
set "REPORT_DIR=%WORK_ROOT%\reports"
set "SYNC_ARCHIVE=%WORK_ROOT%\synced\%RUN_ID%"

if not exist "%WORK_ROOT%" mkdir "%WORK_ROOT%"
if not exist "%LOCAL_INPUT%" mkdir "%LOCAL_INPUT%"
if not exist "%LOCAL_OUTPUT%" mkdir "%LOCAL_OUTPUT%"
if not exist "%REPORT_DIR%" mkdir "%REPORT_DIR%"

echo.
echo Copying new/changed PDFs from shared folder to local input...
echo From: %SHARED_INPUT%
echo To:   %LOCAL_INPUT%
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
echo Local input:        %AADHAAR_HOST_INPUT%
echo Local output:       %AADHAAR_HOST_OUTPUT%
echo Resume state:       %AADHAAR_HOST_OUTPUT%\_review_resume_state.json
echo Shared output:      %SHARED_OUTPUT%
echo Browser will open after noVNC is ready.
echo.

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

docker compose down >nul 2>nul
docker compose up --force-recreate -d
if errorlevel 1 (
    echo Docker start failed.
    pause
    exit /b 1
)

echo.
echo Waiting for noVNC to become ready...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$u='http://localhost:6080/vnc.html'; $deadline=(Get-Date).AddSeconds(120); while ((Get-Date) -lt $deadline) { try { $r=Invoke-WebRequest -UseBasicParsing -Uri $u -TimeoutSec 2; if ($r.StatusCode -ge 200) { Start-Sleep -Seconds 2; exit 0 } } catch { }; Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 1 (
    echo noVNC did not become ready in time. Recent container logs:
    docker compose logs --tail=100 aadhaar-detector
    goto after_app
)

echo Opening browser:
echo %APP_URL%
start "" "%APP_URL%"

echo.
echo Aadhaar detector is running. Close the app window when review is finished.
docker attach aadhaar-detector

:after_app
echo.
echo Docker app stopped.
docker compose down >nul 2>nul

echo Deleted Aadhaar files are in:
echo %LOCAL_OUTPUT%

if not exist "%SYNC_SCRIPT%" (
    echo Sync script not found:
    echo %SYNC_SCRIPT%
    echo Cannot sync back to shared folders.
    goto end
)

echo.
echo Syncing deleted and masked files back to shared drive...
echo Deleted files will be copied to: %SHARED_OUTPUT%
echo Matching originals will be removed from: %SHARED_INPUT%
echo Masked files will replace originals in: %SHARED_INPUT%
echo.

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
echo.
echo Deleted files copied to shared output:
powershell -NoProfile -ExecutionPolicy Bypass -Command "$report=Get-ChildItem -LiteralPath $env:REPORT_DIR -Filter 'sync_deleted_*.csv' ^| Sort-Object LastWriteTime -Descending ^| Select-Object -First 1; if ($report) { Import-Csv -LiteralPath $report.FullName ^| Where-Object { $_.Operation -eq 'Delete' -and $_.Status -like 'COPIED_*' } ^| ForEach-Object { Write-Host ('  ' + $_.SharedOutput) } }"
echo.
echo Archiving synced local output while keeping resume state...
if not exist "%SYNC_ARCHIVE%" mkdir "%SYNC_ARCHIVE%"
for %%F in ("%LOCAL_OUTPUT%\*.pdf") do if exist "%%~fF" move /Y "%%~fF" "%SYNC_ARCHIVE%\" >nul
if exist "%LOCAL_OUTPUT%\_deleted_manifest.json" move /Y "%LOCAL_OUTPUT%\_deleted_manifest.json" "%SYNC_ARCHIVE%\" >nul
if exist "%LOCAL_OUTPUT%\_masked_keep_manifest.json" move /Y "%LOCAL_OUTPUT%\_masked_keep_manifest.json" "%SYNC_ARCHIVE%\" >nul
if exist "%LOCAL_OUTPUT%\mask_keep_session.json" move /Y "%LOCAL_OUTPUT%\mask_keep_session.json" "%SYNC_ARCHIVE%\" >nul
if exist "%LOCAL_OUTPUT%\.masked_keep_tmp" rmdir /s /q "%LOCAL_OUTPUT%\.masked_keep_tmp"
echo Synced archive: %SYNC_ARCHIVE%
echo Resume state kept: %LOCAL_OUTPUT%\_review_resume_state.json

:end
pause
