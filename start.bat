@echo off
chcp 65001 >nul
title PureSearch - Local Aggregation Search
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [!] Python not found.
  echo     Install Python 3.9+ from https://www.python.org/downloads/
  echo     and tick "Add python.exe to PATH" during install.
  pause
  exit /b 1
)

echo.
echo  ============================================
echo    PureSearch  is starting...
echo    Browser will open automatically.
echo    Close this window to stop the server.
echo  ============================================
echo.

python server.py
pause
