@echo off
setlocal
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" (
  echo Python 3.13 not found.
  pause
  exit /b 1
)
"%PY%" test_runner.py
pause
