@echo off
cd /d "%~dp0"
if not exist ".env" (
  echo FEHLER: .env fehlt. Kopiere .env.example nach .env und trage die Werte ein.
  pause
  exit /b 1
)
python morgenreport.py %*
pause
