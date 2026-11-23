@echo off
setlocal
cd /d "%~dp0"
set "PY=%~dp0.corevenv\Scripts\python.exe"
if not exist "%PY%" exit /b 3
"%PY%" docs\reference\manufacturing_ai_v06_reference_invariants.py -q
if errorlevel 1 exit /b 1
"%PY%" -m unittest -v tests_pg.test_policy_rls
exit /b %ERRORLEVEL%
