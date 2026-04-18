@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion

REM === RDC full pipeline: collect + analyze ===
REM Usage: rdc-report.bat <capture.rdc> [-j WORKERS]

set "SCRIPT_DIR=%~dp0"

REM === Find Python (embedded first, then system) ===
set "PYTHON="
set "EMBEDDED_PY=%SCRIPT_DIR%..\python\python.exe"
if exist "%EMBEDDED_PY%" (
    "%EMBEDDED_PY%" -c "print('ok')" >nul 2>&1
    if !errorlevel! equ 0 set "PYTHON=!EMBEDDED_PY!"
)
if not defined PYTHON (
    where python >nul 2>&1
    if !errorlevel! equ 0 set "PYTHON=python"
)
if not defined PYTHON (
    echo [ERROR] No Python found ^(embedded or system^).
    exit /b 1
)

REM === Validate capture argument ===
if "%~1"=="" (
    echo Usage: rdc-report.bat ^<capture.rdc^> [-j WORKERS]
    exit /b 1
)

set "CAPTURE=%~f1"
if not exist "%CAPTURE%" (
    echo [ERROR] File not found: %CAPTURE%
    exit /b 1
)

REM === Derive analysis directory name ===
set "CAPTURE_STEM=%~n1"
set "CAPTURE_DIR=%~dp1"
set "ANALYSIS_DIR=%CAPTURE_DIR%%CAPTURE_STEM%-analysis"

REM === Collect remaining args (skip first) ===
set "EXTRA_ARGS="
set "SKIP=1"
for %%a in (%*) do (
    if !SKIP! equ 0 (
        set "EXTRA_ARGS=!EXTRA_ARGS! %%a"
    ) else (
        set "SKIP=0"
    )
)

REM === Phase 1: Collect ===
echo ================================================================
echo  Phase 1: Data Collection
echo ================================================================
"%PYTHON%" "%SCRIPT_DIR%rdc\collect.py" "%CAPTURE%" -j 8%EXTRA_ARGS%
if !errorlevel! neq 0 (
    echo [ERROR] collect.py failed.
    exit /b 1
)

REM === Phase 2: Analyze ===
echo.
echo ================================================================
echo  Phase 2: Performance Report
echo ================================================================
"%PYTHON%" "%SCRIPT_DIR%rdc\analyze.py" "%ANALYSIS_DIR%"
if !errorlevel! neq 0 (
    echo [ERROR] analyze.py failed.
    exit /b 1
)

echo.
echo ================================================================
echo  Done! Report: %ANALYSIS_DIR%\performance_report.html
echo ================================================================
