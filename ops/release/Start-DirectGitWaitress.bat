@echo off
setlocal EnableExtensions

REM Native Node tools emit UTF-8. Match the console code page and disable ANSI
REM colors so PowerShell 5 does not turn build output into mojibake.
chcp 65001 >nul
set "NO_COLOR=1"

REM This file lives in ops\release. Resolve the Git worktree without relying on
REM the caller's working directory or an activated virtual environment.
for %%I in ("%~dp0\..\..") do set "PROJECT_DIR=%%~fI"
set "PREPARER=%PROJECT_DIR%\ops\release\Invoke-DirectGitUpdateAndPrepare.ps1"
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
if not exist "%PREPARER%" (
    echo [ERROR] Update and preparation workflow is missing: %PREPARER%
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass ^
    -File "%PREPARER%" ^
    -RepositoryRoot "%PROJECT_DIR%"

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] FFXIVShare update or preparation exited with code %EXIT_CODE%.
    pause
    exit /b %EXIT_CODE%
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
