@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "REPO_DIR=%%~fI"

cd /d "%REPO_DIR%"
if not exist "logs" mkdir "logs"

cloudflared tunnel run brownberries >> "%REPO_DIR%\logs\windows-tunnel.log" 2>&1

