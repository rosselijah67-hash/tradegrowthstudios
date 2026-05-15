@echo off
setlocal

cd /d "%~dp0\.."
set "DASHBOARD_URL=http://127.0.0.1:8787"

echo.
echo Starting Local Lead Dashboard
echo Project: %CD%
echo URL: %DASHBOARD_URL%
echo.
echo If the browser does not open, open this URL manually:
echo %DASHBOARD_URL%
echo.
echo Leave this window open while you use the dashboard.
echo Press Ctrl+C in this window to stop the dashboard.
echo.

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
    set "PYTHON_EXE=.venv\Scripts\python.exe"
    echo Using local virtual environment.
)

if not defined PYTHON_EXE (
    where python >nul 2>nul && set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE (
    where py >nul 2>nul && set "PYTHON_EXE=py"
)

if not defined PYTHON_EXE (
    echo Python was not found. Create .venv or install Python, then run this file again.
    pause
    exit /b 1
)

start "" "%DASHBOARD_URL%"

%PYTHON_EXE% -m src.dashboard_app --host 127.0.0.1 --port 8787

if errorlevel 1 (
    echo.
    echo Dashboard stopped with an error. Check the message above.
    pause
)

endlocal
