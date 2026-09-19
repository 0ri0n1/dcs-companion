@echo off
echo This mode can send one explicitly confirmed F-22 navigation action after DCS gains focus.
echo Flight controls and hazardous actions remain display-only.
choice /C YN /N /M "Enable live controls for this run? (Y/N): "
if errorlevel 2 exit /b
cd /d "%~dp0.."
"%~dp0.venv\Scripts\python.exe" "%~dp0launcher.py" start --arm
if errorlevel 1 pause
