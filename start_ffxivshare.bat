@echo off
setlocal EnableExtensions

REM This is the only production start entry point. It checks the database and
REM offers a verified upgrade when migrations are pending.
call "%~dp0ops\release\Start-DirectGitWaitress.bat"
exit /b %ERRORLEVEL%
