@echo off
setlocal EnableExtensions

REM This file lives in ops\release. Resolve the Git worktree without relying on
REM the caller's working directory or an activated virtual environment.
for %%I in ("%~dp0\..\..") do set "PROJECT_DIR=%%~fI"
set "LAUNCHER=%PROJECT_DIR%\ops\release\Invoke-DirectGitLauncher.ps1"

if not exist "%PROJECT_DIR%\manage.py" (
    echo [ERROR] Git deployment root is invalid: %PROJECT_DIR%
    pause
    exit /b 1
)
if not exist "%LAUNCHER%" (
    echo [ERROR] Unified launcher is missing: %LAUNCHER%
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass ^
    -File "%LAUNCHER%" ^
    -RepositoryRoot "%PROJECT_DIR%"

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] FFXIVShare launcher exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
