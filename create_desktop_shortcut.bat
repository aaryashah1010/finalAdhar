@echo off
setlocal

set "TARGET_BAT=%~dp0run_docker_staged.bat"
set "TARGET_DIR=%~dp0"
set "SHORTCUT_NAME=Aadhaar Detector.lnk"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$target=$env:TARGET_BAT; $workdir=$env:TARGET_DIR; $desktop=[Environment]::GetFolderPath('Desktop'); $link=Join-Path $desktop $env:SHORTCUT_NAME; $shell=New-Object -ComObject WScript.Shell; $shortcut=$shell.CreateShortcut($link); $shortcut.TargetPath=$target; $shortcut.WorkingDirectory=$workdir; $shortcut.IconLocation=$env:SystemRoot + '\System32\shell32.dll,167'; $shortcut.Save(); Write-Host ('Created shortcut: ' + $link)"
if errorlevel 1 (
    echo Could not create desktop shortcut.
    pause
    exit /b 1
)

pause
