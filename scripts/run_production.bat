@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" exit /b 3
".venv\Scripts\python.exe" "main.py" --run
exit /b %errorlevel%
