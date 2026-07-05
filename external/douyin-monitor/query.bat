@echo off
chcp 65001 >nul 2>&1
echo ==========================================
echo   Douyin Monitor - Query Profile
echo ==========================================
echo.
python main.py query --interactive
pause
