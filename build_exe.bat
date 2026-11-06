@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [!] Run install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install pyinstaller==6.22.3
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --console --name coin-ai --add-data "dashboard.html;." --add-data ".env.example;." --collect-submodules anthropic main.py
if errorlevel 1 (
  echo [!] Build failed.
  pause
  exit /b 1
)
copy /y "dist\coin-ai.exe" "coin-ai.exe" >nul
echo [OK] coin-ai.exe is ready in this folder. Double-click it to start.
pause
