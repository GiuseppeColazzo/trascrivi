@echo off
REM ============================================================================
REM  Trascrivi - avvio della piattaforma web (Windows)
REM  Doppio click: al primo avvio prepara l'ambiente, poi apre il browser su
REM  http://127.0.0.1:8000
REM ============================================================================
setlocal
cd /d "%~dp0"

set VENV=.venv
set PY=%VENV%\Scripts\python.exe

if not exist "%PY%" (
  echo [1/3] Creo l'ambiente virtuale ^(riuso i pacchetti di sistema: torch, faster-whisper...^)
  where py >nul 2>nul
  if %errorlevel%==0 (
    py -3 -m venv --system-site-packages "%VENV%"
  ) else (
    python -m venv --system-site-packages "%VENV%"
  )
  if not exist "%PY%" (
    echo ERRORE: non riesco a creare l'ambiente virtuale. Serve Python 3.10+ nel PATH.
    pause
    exit /b 1
  )
)

REM Marcatore: le dipendenze si installano una volta sola. Cancellalo per
REM forzare una reinstallazione.
if not exist "%VENV%\.deps-ok" (
  echo [2/3] Installo le dipendenze ^(la prima volta puo' richiedere qualche minuto^)
  REM uv e' ordini di grandezza piu' veloce di pip quando il venv vede i
  REM pacchetti di sistema; se non c'e', si usa pip.
  set UV_CACHE_DIR=%CD%\.uvcache
  "%PY%" -m uv pip install --python "%PY%" -r requirements.txt
  if errorlevel 1 (
    echo    uv non disponibile, uso pip ^(piu' lento: deve risolvere le dipendenze
    echo    contro i pacchetti di sistema^)
    "%PY%" -m pip install --disable-pip-version-check -r requirements.txt
  )
  if errorlevel 1 (
    echo ERRORE: installazione delle dipendenze fallita.
    pause
    exit /b 1
  )
  echo ok> "%VENV%\.deps-ok"
)

echo [3/3] Avvio il server...
"%PY%" trascrivi_app.py %*
echo.
echo Server fermato.
pause
