@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install_autostart.ps1" -StartNow
pause
