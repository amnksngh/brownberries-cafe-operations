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
set "HOST=127.0.0.1"
set "PORT=5050"
set "DEBUG=0"

if not exist ".venv\Scripts\waitress-serve.exe" (
  echo waitress-serve not found at "%REPO_DIR%\.venv\Scripts\waitress-serve.exe"
  echo Please run: .venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)

"%REPO_DIR%\.venv\Scripts\waitress-serve.exe" --listen=127.0.0.1:5050 --threads=12 wsgi:app >> "%REPO_DIR%\logs\windows-app.log" 2>&1
