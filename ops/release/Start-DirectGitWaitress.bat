@echo off
setlocal EnableExtensions

REM Native Node tools emit UTF-8. Match the console code page and disable child
REM process ANSI sequences. The PowerShell launcher adds host-native colors.
chcp 65001 >nul
set "NO_COLOR=1"

REM This file lives in ops\release. Resolve the Git worktree without relying on
REM the caller's working directory or an activated virtual environment.
for %%I in ("%~dp0\..\..") do set "PROJECT_DIR=%%~fI"
set "PREPARER=%PROJECT_DIR%\ops\release\Invoke-DirectGitUpdateAndPrepare.ps1"
set "LAUNCHER=%PROJECT_DIR%\ops\release\Invoke-DirectGitLauncher.ps1"

if not exist "%PROJECT_DIR%\manage.py" (
    powershell.exe -NoProfile -Command ^
        "Write-Host ('[ERROR] Git deployment root is invalid: ' + $env:PROJECT_DIR) -ForegroundColor Red"
    pause
    exit /b 1
)
if not exist "%LAUNCHER%" (
    powershell.exe -NoProfile -Command ^
        "Write-Host ('[ERROR] Unified launcher is missing: ' + $env:LAUNCHER) -ForegroundColor Red"
    pause
    exit /b 1
)
if not exist "%PREPARER%" (
    powershell.exe -NoProfile -Command ^
        "Write-Host ('[ERROR] Update and preparation workflow is missing: ' + $env:PREPARER) -ForegroundColor Red"
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass ^
    -File "%PREPARER%" ^
    -RepositoryRoot "%PROJECT_DIR%"

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    powershell.exe -NoProfile -Command ^
        "Write-Host ('[ERROR] FFXIVShare update or preparation exited with code ' + $env:EXIT_CODE + '.') -ForegroundColor Red"
    pause
    exit /b %EXIT_CODE%
)

powershell.exe -NoProfile -ExecutionPolicy Bypass ^
    -File "%LAUNCHER%" ^
    -RepositoryRoot "%PROJECT_DIR%"

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    powershell.exe -NoProfile -Command ^
        "Write-Host ('[ERROR] FFXIVShare launcher exited with code ' + $env:EXIT_CODE + '.') -ForegroundColor Red"
    pause
)
exit /b %EXIT_CODE%
