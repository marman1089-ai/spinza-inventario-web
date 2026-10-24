@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python non trovato. Installa Python 3.11 e riprova.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  py -3.11 -m venv .venv
  if errorlevel 1 goto :errore
)
call ".venv\Scripts\activate.bat"
python -m pip install -r requirements.txt
if errorlevel 1 goto :errore
start "" http://127.0.0.1:8000
python run.py
goto :fine
:errore
echo.
echo Avvio non riuscito. Controlla AVVIO_RIPARATO.txt.
pause
:fine
