@echo off
setlocal

set "TARGET_BAT=%~dp0run_docker_staged.bat"
set "TARGET_DIR=%~dp0"
set "SHORTCUT_NAME=Aadhaar Detector.lnk"

echo Select where the Aadhaar Detector shortcut should be created.
echo You can choose Desktop, T: drive, or any shared folder where you have write access.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.Windows.Forms; $target=$env:TARGET_BAT; $workdir=$env:TARGET_DIR; $name=$env:SHORTCUT_NAME; $dialog=New-Object System.Windows.Forms.FolderBrowserDialog; $dialog.Description='Select folder where Aadhaar Detector shortcut should be created'; $dialog.ShowNewFolderButton=$true; if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { Write-Host 'No folder selected.'; exit 2 }; $dir=$dialog.SelectedPath; if (-not (Test-Path -LiteralPath $dir -PathType Container)) { throw ('Selected folder does not exist: ' + $dir) }; $link=Join-Path $dir $name; $shell=New-Object -ComObject WScript.Shell; $shortcut=$shell.CreateShortcut($link); $shortcut.TargetPath=$target; $shortcut.WorkingDirectory=$workdir; $shortcut.IconLocation=(Join-Path $env:SystemRoot 'System32\shell32.dll') + ',167'; $shortcut.Save(); if (-not (Test-Path -LiteralPath $link -PathType Leaf)) { throw 'Shortcut file was not created.' }; Write-Host ('Created shortcut: ' + $link)"
if errorlevel 1 (
    echo Could not create shortcut.
    echo You can still run the app with run_docker_staged.bat.
    pause
    exit /b 1
)

pause
