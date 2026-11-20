@echo off
chcp 65001 >nul
title PureSearch - update hot words
cd /d "%~dp0"
echo Updating today's hot words...
python update_daily.py
echo.
echo Done. Refresh the page to see new words.
timeout /t 6 >nul
