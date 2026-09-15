@echo off
cd /d "%~dp0"

.venv\Scripts\python.exe main_auto_reply.py
exit /b %ERRORLEVEL%
