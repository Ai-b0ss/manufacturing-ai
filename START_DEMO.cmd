@echo off
setlocal
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" (
  echo Python 3.13 installation is required.
  pause
  exit /b 1
)
if not exist "data\engineering_corpus.sqlite3" (
  echo P7 index missing. Run IMPORT_CORPUS.cmd.
  pause
  exit /b 1
)
if not exist "data\engineering_inspection.sqlite3" (
  echo P8 inspection index missing. Run IMPORT_CORPUS.cmd.
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:19134/api/health' -TimeoutSec 2; if ($r.Content -match 'drawing-vector-cad-candidates') {exit 0} else {exit 2} } catch {exit 1}" >nul 2>&1
if "%ERRORLEVEL%"=="0" (
  echo P13 already running: http://127.0.0.1:19134/
  exit /b 0
)
if "%ERRORLEVEL%"=="2" (
  echo Port 19134 is occupied by another application. No process will be stopped.
  pause
  exit /b 2
)
echo P13 starts on http://127.0.0.1:19134/
echo Educational roles only; do not load confidential files.
"%PY%" app.py --host 127.0.0.1 --port 19134
if errorlevel 1 pause
