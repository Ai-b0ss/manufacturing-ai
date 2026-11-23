@echo off
setlocal
cd /d "%~dp0"
set "PY=%~dp0.corevenv\Scripts\python.exe"
if not exist "%PY%" (
  echo Core v0.6 virtualenv missing. Install dependencies from requirements-core.txt.
  exit /b 3
)
"%PY%" tools\bootstrap_portable_pg.py
if errorlevel 1 exit /b 1
"%PY%" tools\provision_core.py
if errorlevel 1 exit /b 2
"%PY%" -m core06.migrate_demo
if errorlevel 1 exit /b 3
echo Core v0.6 PostgreSQL initialized. No old demo servers were modified.
exit /b 0
