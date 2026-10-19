@echo off
chcp 65001 > nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Telegram Downloader Bot

echo ===================================================
echo [STARTING] Telegram Downloader Bot...
echo ===================================================

where python >nul 2>nul
if %errorlevel% neq 0 (
    where py >nul 2>nul
    if %errorlevel% equ 0 (
        py bot.py
    ) else (
        echo [ERROR] Python is not installed or not in PATH!
        pause
        exit /b 1
    )
) else (
    python bot.py
)

if errorlevel 1 (
    echo.
    echo ===================================================
    echo [ERROR] Bot stopped with an error!
    echo Please check the error message above or your .env file.
    echo ===================================================
)

pause
