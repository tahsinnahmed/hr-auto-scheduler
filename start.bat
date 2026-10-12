@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   HR Auto Scheduler © Copyright: Tahsin Ahmed
echo ============================================================
echo.

REM ---------------------------------------------------------------------------
REM 1. Check Python is installed
REM ---------------------------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on your PATH.
    echo         Install Python 3.10+ from https://www.python.org/downloads/
    echo         During installation, check "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Python %PYVER% found

REM ---------------------------------------------------------------------------
REM 2. Create virtual environment if missing
REM ---------------------------------------------------------------------------
if not exist "venv\Scripts\python.exe" (
    echo [SETUP] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created
) else (
    echo [OK] Virtual environment already exists
)

REM ---------------------------------------------------------------------------
REM 3. Activate the virtual environment
REM ---------------------------------------------------------------------------
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM 4. Install / verify requirements
REM ---------------------------------------------------------------------------
if not exist "requirements.txt" (
    echo [ERROR] requirements.txt not found in the project folder.
    pause
    exit /b 1
)

echo [CHECK] Verifying Python dependencies...
pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
    echo [WARN] Some dependencies failed to install. Retrying with verbose output...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. Check your internet connection.
        pause
        exit /b 1
    )
)
echo [OK] All dependencies satisfied

REM ---------------------------------------------------------------------------
REM 5. Run the launcher
REM ---------------------------------------------------------------------------
echo.
python start.py

REM ---------------------------------------------------------------------------
REM 6. Keep the window open if something went wrong
REM ---------------------------------------------------------------------------
pause