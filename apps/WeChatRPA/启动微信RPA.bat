@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0runtime"

echo [check] using embedded Python...
set "PYW=%~dp0runtime\python\pythonw.exe"

if not exist "!PYW!" (
  echo [error] embedded pythonw.exe not found: !PYW!
  pause
  exit /b 1
)

echo [check] verifying gui launcher...
if not exist "gui.py" (
  echo [error] gui.py not found
  pause
  exit /b 1
)

echo [start] launching 销售策略与高情商沟通官 desktop app...
start "" "!PYW!" gui.py
exit /b 0
