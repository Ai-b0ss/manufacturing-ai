@echo off
setlocal
cd /d "%~dp0"
"%LOCALAPPDATA%\Programs\Python\Python313\python.exe" tools\bringup_factory.py --start
exit /b %ERRORLEVEL%
