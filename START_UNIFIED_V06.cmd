@echo off
setlocal
cd /d "%~dp0"
set "PY=C:\Users\user\Documents\ManufacturingAI-Core-V06-P3-Hybrid-Retrieval\.p3venv\Scripts\python.exe"
if not exist "%PY%" (
 echo P3 environment missing; consult UNIFIED_INTEGRATION_STATUS.md.
 exit /b 3
)
set "FACTORY_P2_APP_ORIGIN=http://localhost:19139"
set "FACTORY_P2_WEB_CLIENT=factory-web-unified"
rem RLS migrations checked on 2026-11-18; protected upload enabled.
set "FACTORY_V06_INGEST_ENABLED=1"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "OMP_NUM_THREADS=2"
set "MKL_NUM_THREADS=2"
set "TOKENIZERS_PARALLELISM=false"
"%PY%" -m uvicorn unified_app:app --host 127.0.0.1 --port 19139 --no-access-log
exit /b %ERRORLEVEL%
