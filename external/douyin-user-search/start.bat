@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  Douyin User Search - Launcher (raw output, no cleaning)
REM  Flow: install deps -> login once -> search -> raw JSON
REM ============================================================

cd /d "%~dp0"

echo.
echo ============================================
echo   Douyin User Search (raw output)
echo ============================================
echo.

REM ---- check python ----
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo Please install Python and add it to PATH.
    pause
    exit /b 1
)

REM ---- check / install playwright ----
python -c "import playwright" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Playwright not installed. Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)

REM ---- ensure chromium kernel ----
python -m playwright install chromium >nul 2>nul

:menu
echo.
echo --- MENU ---
echo   1) First-time LOGIN (scan QR code once)
echo   2) SEARCH users (raw JSON, no cleaning)
echo   3) SEARCH + load more (scroll up to ~36)
echo   4) Exit
echo.
set /p choice="Enter choice [1-4]: "
if "!choice!"=="1" goto do_login
if "!choice!"=="2" goto do_search
if "!choice!"=="3" goto do_search_more
if "!choice!"=="4" exit /b 0
echo Invalid choice.
goto menu

:do_login
echo.
echo Launching visible browser for login...
echo Log in with QR code, then come back and press Enter. Login is saved to .\douyin_profile
echo.
python login.py
echo.
pause
goto menu

:do_search_more
set MORE=--more
goto do_search_run

:do_search
set MORE=

:do_search_run
echo.
set /p keyword="Enter search keyword: "
if "!keyword!"=="" (
    echo [ERROR] keyword cannot be empty.
    goto do_search_run
)

echo.
echo --- mode ---
echo   1) Visible browser  (default, recommended)
echo   2) Headless         (background; needs login already saved)
set /p mode="Enter [1-2] (default 1): "
if "!mode!"=="" set mode=1
set HEAD=
if "!mode!"=="2" set HEAD=--headless

echo.
echo Launching search for: !keyword!
echo --------------------------------------------
python raw.py "!keyword!" !MORE! !HEAD!
echo --------------------------------------------
echo.
echo Done. Raw JSON printed above and saved in results folder.
pause
goto menu
