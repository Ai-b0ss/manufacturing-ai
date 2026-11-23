@echo off
setlocal
cd /d "%~dp0"
set "NODE=K:\FF-Windows-Bridge\state\universal-agent-canary-688ef3e\tools\node\node-v22.14.0-win-x64\node.exe"
set "PY=C:\Users\user\Documents\ManufacturingAI-Core-V06-P3-Hybrid-Retrieval\.p3venv\Scripts\python.exe"
if not exist "%NODE%" (echo Node test runtime missing & exit /b 3)
if not exist "%PY%" (echo Python test runtime missing & exit /b 3)
echo [1/9] Browser engineer
"%NODE%" tests_pg\browser_unified_acceptance.cjs
if errorlevel 1 exit /b 1
echo [2/9] Browser technologist
"%NODE%" tests_pg\browser_unified_technologist_acceptance.cjs
if errorlevel 1 exit /b 2
echo [3/9] Browser finance
"%NODE%" tests_pg\browser_unified_finance_acceptance.cjs
if errorlevel 1 exit /b 3
echo [4/9] Browser director
"%NODE%" tests_pg\browser_unified_director_acceptance.cjs
if errorlevel 1 exit /b 4
echo [5/9] Browser mobile
"%NODE%" tests_pg\browser_unified_mobile_acceptance.cjs
if errorlevel 1 exit /b 5
echo [6/9] RLS and API tests
"%PY%" -m unittest -v tests_integration.test_unified_live
if errorlevel 1 exit /b 6
echo [7/9] Hybrid search tests
"%PY%" -m tools.unified_search_acceptance
if errorlevel 1 exit /b 7
echo [8/9] Deep Chrome PDF/STEP/CAD provenance and ambiguity
"%NODE%" tests_pg\browser_unified_cad_acceptance.cjs
if errorlevel 1 exit /b 8
echo [9/9] Engineering BOM/PDF/STEP integrity and source ACL
"%PY%" -m unittest -v tests_integration.test_p13_source_integrity
if errorlevel 1 exit /b 9
echo ALL AUTOMATED ACCEPTANCE TESTS PASS
exit /b 0
