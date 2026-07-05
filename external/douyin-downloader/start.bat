@echo off
chcp 65001 >nul
setlocal

title Douyin Downloader

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found
    pause
    exit /b 1
)

if not exist "%~dp0main.py" (
    echo [ERROR] main.py not found in: %~dp0
    pause
    exit /b 1
)

echo.
echo   ================================
echo    Douyin Video/Image Downloader
echo   ================================
echo.

set "INPUT="
set /p INPUT=Paste URL, share text, or file path:
if "%INPUT%"=="" (
    echo [ERROR] Input cannot be empty
    echo.
    pause
    exit /b 1
)

echo.
echo   1. Video only
echo   2. Stats JSON only
echo   3. All (video + stats JSON in folder)
echo.

set "MODE="
set /p MODE=Choose mode [1/2/3, default=3]:
if "%MODE%"=="" set "MODE=1"

echo.
set "COMMENTS="
set /p COMMENTS=Also scrape comments? [y/N, default=N]:
if /i "%COMMENTS%"=="y" (
    set "COMMENTS_FLAG=-c"
) else (
    set "COMMENTS_FLAG="
)

echo.
set "WORKERS="
set /p WORKERS=Parallel threads [1-8, default=1]:
if "%WORKERS%"=="" set "WORKERS=1"

echo.
set "OUTPUT_DIR="
set /p OUTPUT_DIR=Save to [Enter for default: .\downloads]:
if "%OUTPUT_DIR%"=="" set "OUTPUT_DIR=.\downloads"

echo.
echo [INFO] Input:    %INPUT%
echo [INFO] Mode:     %MODE%
echo [INFO] Comments: %COMMENTS_FLAG%
echo [INFO] Workers:  %WORKERS%
echo [INFO] Save:     %OUTPUT_DIR%
echo [INFO] Downloading, please wait...
echo.

python "%~dp0main.py" --input "%INPUT%" -m %MODE% %COMMENTS_FLAG% -w %WORKERS% -o "%OUTPUT_DIR%"

if errorlevel 1 (
    echo.
    echo [ERROR] Download failed
) else (
    echo.
    echo [OK] Download completed
)

endlocal
pause
