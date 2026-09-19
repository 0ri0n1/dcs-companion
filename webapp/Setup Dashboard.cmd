@echo off
cd /d "%~dp0.."
if not exist "%~dp0.venv\Scripts\python.exe" python -m venv "%~dp0.venv"
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements-lock.txt"
if errorlevel 1 goto failed
pushd "%~dp0frontend"
call npm ci
if errorlevel 1 goto failed
call npm run build
if errorlevel 1 goto failed
popd
echo Setup complete. Open Start Dashboard.cmd.
echo The companion will discover DCS and build data for available aircraft automatically.
echo If you have several DCS profiles, choose one in Your setup.
pause
exit /b 0
:failed
echo Setup stopped. Read the message above before retrying.
pause
exit /b 1
