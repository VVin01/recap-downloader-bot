@echo off
chcp 65001 > nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Media Downloader CLI

where python >nul 2>nul
if %errorlevel% neq 0 (
    where py >nul 2>nul
    if %errorlevel% equ 0 (
        py cli.py
    ) else (
        echo [ERROR] Python is not installed or not in PATH!
        pause
        exit /b 1
    )
) else (
    python cli.py
)

pause
