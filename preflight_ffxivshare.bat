@echo off
setlocal EnableExtensions

REM Read-only release readiness check. The only write is a redacted report in
REM the drive-level FFXIVShare-R20\Readiness directory.
for %%I in ("%~dp0.") do set "PROJECT_DIR=%%~fI"
set "PREFLIGHT=%PROJECT_DIR%\ops\release\Invoke-DirectGitReleaseReadiness.ps1"

if not exist "%PREFLIGHT%" (
    echo [ERROR] Release readiness wrapper is missing: %PREFLIGHT%
    pause
    exit /b 1
)

set "TARGET_COMMIT=%FFXIVSHARE_TARGET_COMMIT%"
if not defined TARGET_COMMIT (
    set /p "TARGET_COMMIT=Enter the approved 40-character target commit SHA: "
)
if not defined TARGET_COMMIT (
    echo [ERROR] A target commit SHA is required.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass ^
    -File "%PREFLIGHT%" ^
    -RepositoryRoot "%PROJECT_DIR%" ^
    -TargetCommit "%TARGET_COMMIT%"

set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" (
    echo [OK] Release readiness checks passed.
) else if "%EXIT_CODE%"=="2" (
    echo [NO-GO] Review the blockers in the generated readiness report.
) else (
    echo [ERROR] Release readiness failed with code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
