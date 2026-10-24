@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo   HFSYNC: Encrypted Video Uploader
echo ========================================================

if not exist "videos_to_sync" (
    mkdir "videos_to_sync"
    echo Created 'videos_to_sync' folder.
    echo Put your video files or folders inside 'videos_to_sync' and run this script again.
    pause
    exit /b
)

C:\Users\opc\python_env\python.exe uploader.py --input-dir videos_to_sync
pause
