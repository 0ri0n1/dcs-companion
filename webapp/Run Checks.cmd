@echo off
cd /d "%~dp0.."
"%~dp0.venv\Scripts\python.exe" -m pytest -c "%~dp0pytest.ini" "%~dp0tests" -q
pause
