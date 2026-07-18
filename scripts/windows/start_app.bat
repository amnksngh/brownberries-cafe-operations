@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "REPO_DIR=%%~fI"

cd /d "%REPO_DIR%"
if not exist "logs" mkdir "logs"

if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment not found at "%REPO_DIR%\.venv\Scripts\python.exe"
  exit /b 1
)

set "PYTHONUNBUFFERED=1"
"%REPO_DIR%\.venv\Scripts\python.exe" "%REPO_DIR%\run.py" >> "%REPO_DIR%\logs\windows-app.log" 2>&1

