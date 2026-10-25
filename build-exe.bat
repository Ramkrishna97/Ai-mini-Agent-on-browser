@echo off
setlocal
cd /d "%~dp0"
title Build Local Workflow executable
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\build.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Build failed. See the error above.
pause
exit /b %RESULT%
