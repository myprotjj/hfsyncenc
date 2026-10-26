@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo   Starting Automated Drop Folder Watcher Service
echo ========================================================
echo.
echo Folders monitored:
echo   - watch_encrypt_sparse\
echo   - watch_encrypt_password\
echo   - watch_decrypt\
echo.
echo Drop any folder or file into these directories to process.
echo Press Ctrl+C in this window to stop.
echo ========================================================

if exist "C:\Users\opc\python_env\python.exe" (
    "C:\Users\opc\python_env\python.exe" -u drop_watcher.py %*
) else (
    python -u drop_watcher.py %*
)
pause
