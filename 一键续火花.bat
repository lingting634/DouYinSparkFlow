@echo off
cd /d "%~dp0"

echo ============================================
echo   DouYin Auto Spark - Running
echo   Please wait (may take 2-4 minutes)...
echo ============================================
echo.

.venv\Scripts\python.exe main.py
set EXITCODE=%ERRORLEVEL%

echo.
echo ============================================
echo   Task finished. Exit code: %EXITCODE%
if %EXITCODE% NEQ 0 (
    echo   *** An error occurred! Check above. ***
) else (
    echo   Check logs\app.log for details.
)
echo ============================================
echo Press any key to close...
pause >nul
exit /b %EXITCODE%