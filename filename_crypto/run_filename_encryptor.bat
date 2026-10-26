@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo   HFSYNC: Reversible Filename Encryptor / Decryptor
echo ========================================================

if exist "C:\Users\opc\python_env\python.exe" (
    "C:\Users\opc\python_env\python.exe" filename_encryptor.py %*
) else (
    python filename_encryptor.py %*
)

echo.
pause
