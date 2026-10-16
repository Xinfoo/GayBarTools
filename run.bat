@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run: py scripts\bootstrap.py
  exit /b 1
)
".venv\Scripts\python.exe" -B -m gaybar_tools %*
