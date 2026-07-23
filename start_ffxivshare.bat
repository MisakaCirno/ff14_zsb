@echo off
setlocal EnableExtensions

REM This is the only production start entry point. It can fast-forward to the
REM approved remote commit, prepares assets and dependencies, verifies the
REM release, checks the database, and offers a verified migration when needed.
call "%~dp0ops\release\Start-DirectGitWaitress.bat"
exit /b %ERRORLEVEL%
