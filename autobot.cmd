@echo off
rem auto-bot launcher. Double-click for the console, or: autobot.cmd "your task"
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [auto-bot] First run - setting up Python environment...
  python -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)
if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul
title auto-bot
".venv\Scripts\python.exe" -m autobot %*
exit /b %errorlevel%
:fail
echo [auto-bot] Setup failed. Make sure Python 3.11+ is installed and on PATH.
pause
exit /b 1
