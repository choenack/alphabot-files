@echo off
chcp 65001 >nul
cd /d "%~dp0"
title coin ai
:loop
".venv\Scripts\python.exe" main.py
if errorlevel 2 goto stop
if errorlevel 1 (
  echo [!] coin ai crashed. Restarting in 30 seconds...
  timeout /t 30 /nobreak >nul
  goto loop
)
:stop
echo coin ai stopped.
pause
