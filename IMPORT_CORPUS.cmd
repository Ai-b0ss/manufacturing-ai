@echo off
setlocal
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" (
  echo Python 3.13 missing.
  pause
  exit /b 1
)
if not exist ".p8venv\Scripts\python.exe" (
  echo OpenCascade venv missing. See P13_STATUS.md.
  pause
  exit /b 1
)
"%PY%" engineering_corpus.py import
if errorlevel 1 (
  echo P7 corpus import failed. Existing index was not replaced.
  pause
  exit /b 1
)
"%PY%" engineering_inspection.py import
if errorlevel 1 (
  echo P8 geometry import failed. Existing index was not replaced.
  pause
  exit /b 1
)
echo Both verified engineering indexes rebuilt successfully.
pause
