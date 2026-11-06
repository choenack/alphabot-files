@echo off
cd /d "%~dp0"
if not exist ".git" (
  echo [!] This folder was not installed with git. Download the new ZIP and copy the files over.
  echo     Your .env and coinai.db are kept as long as you do not delete them.
  pause
  exit /b 1
)
git pull
".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo [OK] Updated. Restart run.bat.
pause
