@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  Douyin Login Helper (pure English)
REM  Launches a visible browser; you scan the QR code; login state
REM  is saved to douyin_profile and reused by later searches.
REM ============================================================

cd /d "%~dp0"
chcp 65001 >nul

echo.
echo ============================================================
echo   Douyin Login Helper
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [FAIL] Python not found in PATH.
    pause
    exit /b 10
)

python -c "import playwright" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Playwright not installed. Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [FAIL] Failed to install dependencies.
        pause
        exit /b 11
    )
)
python -m playwright install chromium >nul 2>nul

echo A Chrome window will open at douyin.com.
echo   1) Click login if a login modal is not shown
echo   2) Scan the QR code with the Douyin mobile app
echo   3) The window will close automatically once login is detected
echo.

python -X utf8 login.py --timeout 300
set RC=!errorlevel!

echo.
if "!RC!"=="0" (
    echo Login saved. You can now run: test.bat
) else (
    echo Login not detected within timeout ^(exit code !RC!^). Re-run to retry.
)
echo.
pause
exit /b !RC!

endlocal
