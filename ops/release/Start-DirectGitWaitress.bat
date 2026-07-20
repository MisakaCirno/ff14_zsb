@echo off
setlocal EnableExtensions

REM This file lives in ops\release. Resolve the Git worktree without relying on
REM the caller's working directory or an activated virtual environment.
for %%I in ("%~dp0\..\..") do set "PROJECT_DIR=%%~fI"
set "PYTHON=%PROJECT_DIR%\venv\Scripts\python.exe"

if not exist "%PROJECT_DIR%\manage.py" (
    echo [ERROR] Git deployment root is invalid: %PROJECT_DIR%
    pause
    exit /b 1
)
if not exist "%PYTHON%" (
    echo [ERROR] Virtual-environment Python is missing: %PYTHON%
    pause
    exit /b 1
)

cd /d "%PROJECT_DIR%"
if errorlevel 1 (
    echo [ERROR] Could not enter Git deployment root.
    pause
    exit /b 1
)

set "PYTHONUNBUFFERED=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"

echo Checking whether the database schema matches this Git commit...
"%PYTHON%" -B manage.py check_deployment_schema --require-current
if errorlevel 1 (
    echo [ERROR] Waitress was not started.
    echo Run the approved maintenance upgrade workflow if migrations are pending.
    pause
    exit /b 1
)

echo Starting FFXIVShare on http://127.0.0.1:8000/
"%PYTHON%" -B -m waitress ^
    --listen=127.0.0.1:8000 ^
    --threads=4 ^
    --trusted-proxy=127.0.0.1 ^
    "--trusted-proxy-headers=x-forwarded-for x-forwarded-proto" ^
    --clear-untrusted-proxy-headers ^
    --no-expose-tracebacks ^
    ffxivshare.wsgi:application

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] Waitress exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
