@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "REPO_DIR=%%~fI"

cd /d "%REPO_DIR%"
if not exist "logs" mkdir "logs"

where cloudflared >nul 2>nul
if errorlevel 1 (
  echo cloudflared is not installed or not in PATH.
  exit /b 1
)

set "CLOUDFLARED_CONFIG=%USERPROFILE%\.cloudflared\config.yml"
if exist "%CLOUDFLARED_CONFIG%" (
  cloudflared --config "%CLOUDFLARED_CONFIG%" tunnel run brownberries >> "%REPO_DIR%\logs\windows-tunnel.log" 2>&1
) else (
  cloudflared tunnel run brownberries >> "%REPO_DIR%\logs\windows-tunnel.log" 2>&1
)
