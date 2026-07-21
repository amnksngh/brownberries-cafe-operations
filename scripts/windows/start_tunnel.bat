@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "REPO_DIR=%%~fI"

cd /d "%REPO_DIR%"
if not exist "logs" mkdir "logs"

set "LOG_FILE=%REPO_DIR%\logs\windows-tunnel.log"
set "CLOUDFLARED_EXE="
set "CLOUDFLARED_CONFIG="

for %%P in (
  "C:\Program Files (x86)\cloudflared\cloudflared.exe"
  "C:\Program Files\cloudflared\cloudflared.exe"
  "%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe"
) do (
  if not defined CLOUDFLARED_EXE if exist %%~P set "CLOUDFLARED_EXE=%%~P"
)

if not defined CLOUDFLARED_EXE (
  for /f "delims=" %%P in ('where cloudflared 2^>nul') do (
    if not defined CLOUDFLARED_EXE set "CLOUDFLARED_EXE=%%~fP"
  )
)

if not defined CLOUDFLARED_EXE (
  echo [%date% %time%] cloudflared executable not found. >> "%LOG_FILE%"
  exit /b 1
)

for %%P in (
  "%BROWBERRIES_CLOUDFLARED_CONFIG%"
  "%USERPROFILE%\.cloudflared\config.yml"
  "C:\Users\hp\.cloudflared\config.yml"
) do (
  if not defined CLOUDFLARED_CONFIG if exist %%~P set "CLOUDFLARED_CONFIG=%%~P"
)

if defined CLOUDFLARED_CONFIG (
  echo [%date% %time%] Starting cloudflared with config "%CLOUDFLARED_CONFIG%" using "%CLOUDFLARED_EXE%". >> "%LOG_FILE%"
  "%CLOUDFLARED_EXE%" --config "%CLOUDFLARED_CONFIG%" tunnel run brownberries >> "%LOG_FILE%" 2>&1
) else (
  echo [%date% %time%] No config.yml found. Falling back to default cloudflared lookup using "%CLOUDFLARED_EXE%". >> "%LOG_FILE%"
  "%CLOUDFLARED_EXE%" tunnel run brownberries >> "%LOG_FILE%" 2>&1
)
