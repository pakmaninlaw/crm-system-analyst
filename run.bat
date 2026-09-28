@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 main.py
if errorlevel 1 python main.py
pause
