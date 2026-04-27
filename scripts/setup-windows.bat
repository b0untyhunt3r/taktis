@echo off
setlocal enabledelayedexpansion

echo.
echo  taktis Setup (Windows)
echo  ================================
echo.

:: Check for Python 3.10+
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Python not found. Installing via winget...
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    if !errorlevel! neq 0 (
        echo [X] Failed to install Python. Please install Python 3.10+ manually from https://python.org
        pause
        exit /b 1
    )
    echo [i] Please restart this script after Python installation completes.
    pause
    exit /b 0
)

:: Verify Python version >= 3.10
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    if %%a lss 3 (
        echo [X] Python %%a.%%b found, but 3.10+ is required.
        pause
        exit /b 1
    )
    if %%a equ 3 if %%b lss 10 (
        echo [X] Python 3.%%b found, but 3.10+ is required.
        pause
        exit /b 1
    )
)
echo [OK] Python %PYVER%

:: Check for Node.js 18+
where node >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Node.js not found. Installing via winget...
    winget install OpenJS.NodeJS --accept-package-agreements --accept-source-agreements
    if !errorlevel! neq 0 (
        echo [X] Failed to install Node.js. Please install Node.js 18+ manually from https://nodejs.org
        pause
        exit /b 1
    )
    echo [i] Please restart this script after Node.js installation completes.
    pause
    exit /b 0
)
echo [OK] Node.js found

:: Check for Claude Code CLI
where claude >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Claude Code CLI not found. Installing...
    call npm install -g @anthropic-ai/claude-code
    if !errorlevel! neq 0 (
        echo [X] Failed to install Claude Code CLI.
        pause
        exit /b 1
    )
)
echo [OK] Claude Code CLI found

:: Navigate to project root
cd /d "%~dp0\.."

:: Create virtual environment
if not exist .venv (
    echo.
    echo Creating virtual environment...
    python -m venv .venv
)
echo [OK] Virtual environment ready

:: Activate venv and install deps
echo.
echo Installing Python dependencies...
call .venv\Scripts\activate.bat
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [X] Failed to install dependencies.
    pause
    exit /b 1
)
echo [OK] Dependencies installed

:: Check Claude auth
echo.
echo =============================================
echo  Almost done! You need to authenticate with
echo  Claude. Run this command:
echo.
echo    claude login
echo.
echo  (Skip if you already have an API key set
echo   in ANTHROPIC_API_KEY)
echo =============================================
echo.

:: Create start.bat in project root
(
    echo @echo off
    echo cd /d "%%~dp0"
    echo call .venv\Scripts\activate.bat
    echo python run.py
    echo pause
) > start.bat

echo [OK] Setup complete!
echo.
echo To start taktis, run: start.bat
echo Web UI will be at: http://localhost:8080
echo.
pause
