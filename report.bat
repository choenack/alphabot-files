@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py --report 7
echo.
".venv\Scripts\python.exe" main.py --report all
pause
