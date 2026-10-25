@echo off
setlocal
cd /d "%~dp0"
title Local Workflow
echo Starting Local Workflow...

if exist ".venv\Scripts\python.exe" goto dependencies
where py >nul 2>nul
if not errorlevel 1 goto create_with_py
where python >nul 2>nul
if errorlevel 1 goto missing_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 goto missing_python
python -m venv .venv
if errorlevel 1 goto failed
goto dependencies

:create_with_py
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 goto missing_python
py -3 -m venv .venv
if errorlevel 1 goto failed

:dependencies
".venv\Scripts\python.exe" -c "import sys, tkinter, ollama, mcp, playwright, pypdf, docx; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if not errorlevel 1 goto launch
echo Preparing Python dependencies. First launch may take a minute...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "import sys, tkinter, ollama, mcp, playwright, pypdf, docx; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 goto failed

:launch
echo The browser will open automatically. Keep this window open while working.
".venv\Scripts\python.exe" ui_server.py
if errorlevel 1 goto failed
exit /b 0

:missing_python
echo Python 3.12 or newer is required. Install Python with pip and Tcl/Tk support, then double-click this file again.
pause
exit /b 1

:failed
echo.
echo Could not start Local Workflow. See the error above.
pause
exit /b 1
