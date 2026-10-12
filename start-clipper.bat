@echo off
rem Double-click to run Clipper on this computer. First run installs everything (a few minutes).
title Clipper
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
if not exist "clipper\.venv\Scripts\python.exe" (
  echo Setting up Clipper for the first time...
  %PY% -m venv clipper\.venv
  if errorlevel 1 (
    echo.
    echo Python 3.10 or newer is needed. Get it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during install, then run this again.
    pause
    exit /b 1
  )
)
"clipper\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -U pip
"clipper\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -U -r clipper\requirements.txt
if errorlevel 1 (
  echo Installing Clipper's parts failed. Check your internet connection and run this again.
  pause
  exit /b 1
)
"clipper\.venv\Scripts\python.exe" -m clipper.app
pause
