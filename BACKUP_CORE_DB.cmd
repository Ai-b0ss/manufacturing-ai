@echo off
setlocal
cd /d "%~dp0"
if defined FACTORY_BACKUP_PYTHON (
    set "PY=%FACTORY_BACKUP_PYTHON%"
) else (
    set "PY=%~dp0.corevenv\Scripts\python.exe"
    if not exist "%~dp0.corevenv\Scripts\python.exe" set "PY=%~dp0..\ManufacturingAI-Core-V06-P3-Hybrid-Retrieval\.p3venv\Scripts\python.exe"
)
if not exist "%PY%" exit /b 3
"%PY%" -m tools.check_backup_restore
exit /b %ERRORLEVEL%
