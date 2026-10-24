@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo   HFSYNC: Starting All Services
echo ========================================================
echo 1. Starting Jellyfin Streaming Proxy (Port 8080)...
start "HFSYNC Streaming Proxy (Port 8080)" cmd /k "run_proxy.bat"

timeout /t 2 /nobreak >nul

echo 2. Starting Auto-Sync Watcher Service (videos_to_sync)...
start "HFSYNC Auto Watcher Service" cmd /k "run_watcher.bat"

echo.
echo ========================================================
echo Both services are now running in separate windows!
echo - Web Dashboard: http://127.0.0.1:8080/
echo - Drop videos into: %cd%\videos_to_sync
echo ========================================================
