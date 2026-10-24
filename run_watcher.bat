@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo   HFSYNC: High-Speed Multi-Threaded Watcher Service
echo ========================================================
echo Watching folder 'videos_to_sync' with 4 parallel upload threads.
echo.
echo Features:
echo   1. Pipelined in-stream hashing (zero upload delay)
echo   2. Parallel multi-stream uploads (saturates full internet)
echo   3. Instant deduplication & moving duplicates to 'archive/'
echo   4. 100%% anonymized blind hex paths on Hugging Face
echo   5. Automatic Jellyfin .strm generation & local file deletion
echo ========================================================
echo.

C:\Users\opc\python_env\python.exe watcher_service.py --watch-dir videos_to_sync --interval 2 --workers 4
pause
