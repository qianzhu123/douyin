@echo off
chcp 65001 >nul 2>&1
echo ==========================================
echo   Douyin Monitor - Watch Live Status
echo ==========================================
echo.
python main.py watch --interactive
