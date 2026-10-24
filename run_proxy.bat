@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo   HFSYNC: Jellyfin Streaming Proxy Server
echo ========================================================
echo Starting streaming bridge on http://127.0.0.1:8080 ...
echo Web dashboard available at http://127.0.0.1:8080/
echo.

C:\Users\opc\python_env\python.exe stream_proxy.py --port 8080
pause
