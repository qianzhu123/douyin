@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  Douyin User Search - TEST (pure English, minimal)
REM  Just enter a keyword -> search -> show results.
REM ============================================================

cd /d "%~dp0"
chcp 65001 >nul

echo.
echo ============================================================
echo   Douyin User Search
echo ============================================================
echo.

REM ---- 1) check python ----
where python >nul 2>nul
if errorlevel 1 (
    echo [FAIL] Python not found in PATH.
    pause
    exit /b 10
)

REM ---- 2) check / install playwright ----
python -c "import playwright" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Installing dependencies...
    pip install -r requirements.txt
)
python -m playwright install chromium >nul 2>nul

REM ---- 3) ask keyword ----
set /p KW="Enter search keyword: "
if "!KW!"=="" (
    echo [FAIL] keyword cannot be empty.
    pause
    exit /b 1
)

echo.
echo Searching for: !KW!
echo ------------------------------------------------------------
python -X utf8 test_search.py "!KW!"
echo ------------------------------------------------------------
echo.
pause
exit /b !errorlevel!

endlocal
