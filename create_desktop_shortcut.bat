@echo off
setlocal

set "TARGET_BAT=%~dp0run_docker_staged.bat"
set "TARGET_DIR=%~dp0"
set "SHORTCUT_NAME=Aadhaar Detector.lnk"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $target=$env:TARGET_BAT; $workdir=$env:TARGET_DIR; $name=$env:SHORTCUT_NAME; $dirs=@([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonDesktopDirectory'), $workdir); $shell=New-Object -ComObject WScript.Shell; $last=''; foreach ($dir in $dirs) { if ([string]::IsNullOrWhiteSpace($dir) -or -not (Test-Path -LiteralPath $dir -PathType Container)) { continue }; try { $link=Join-Path $dir $name; $shortcut=$shell.CreateShortcut($link); $shortcut.TargetPath=$target; $shortcut.WorkingDirectory=$workdir; $shortcut.IconLocation=(Join-Path $env:SystemRoot 'System32\shell32.dll') + ',167'; $shortcut.Save(); if (-not (Test-Path -LiteralPath $link -PathType Leaf)) { throw 'Shortcut file was not created.' }; Write-Host ('Created shortcut: ' + $link); exit 0 } catch { $last=$_.Exception.Message; Write-Host ('Could not save shortcut in ' + $dir + ': ' + $last) } }; Write-Error ('Could not create shortcut. Last error: ' + $last); exit 1"
if errorlevel 1 (
    echo Could not create desktop shortcut.
    echo You can still run the app with run_docker_staged.bat.
    pause
    exit /b 1
)

pause
