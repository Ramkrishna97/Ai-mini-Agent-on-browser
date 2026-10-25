@echo off
setlocal
cd /d "%~dp0"
title Stop Local Workflow

set "PORT=8765"
set "PID="

for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:":%PORT% .*LISTENING"') do set "PID=%%P"

if not defined PID (
    echo Local Workflow is not running on port %PORT%.
    pause
    exit /b 0
)

echo Stopping Local Workflow (PID %PID%)...
taskkill /PID %PID% /T /F >nul
if errorlevel 1 (
    echo Could not stop the Local Workflow process.
    pause
    exit /b 1
)

echo Local Workflow stopped.
pause
exit /b 0
