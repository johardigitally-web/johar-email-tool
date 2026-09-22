@echo off
REM Start the newsletter tool on http://127.0.0.1:5000
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Eerst eenmalig installeren:
  echo   python -m venv .venv
  echo   .venv\Scripts\python -m pip install -r requirements.txt
  exit /b 1
)
if not exist ".env" (
  echo Geen .env gevonden. Kopieer .env.example naar .env en vul hem in.
)
".venv\Scripts\python.exe" app.py
