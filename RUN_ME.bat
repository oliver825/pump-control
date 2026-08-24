@echo off
cd /d "%~dp0"
title Pump Control

rem --- find Python ---------------------------------------------------------
set PY=
where py >nul 2>nul && set PY=py
if not defined PY (where python >nul 2>nul && set PY=python)

if not defined PY (
  echo.
  echo   Python is not installed on this machine.
  echo.
  echo   Get it from https://www.python.org/downloads/
  echo   During install, TICK THE BOX that says "Add python.exe to PATH".
  echo   You do not need admin rights - choose "Install for me only".
  echo.
  pause
  exit /b 1
)

rem --- make sure the two libraries are there -------------------------------
%PY% -c "import serial, openpyxl" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   First run - installing pyserial and openpyxl. This takes a few seconds.
  echo.
  %PY% -m pip install --user pyserial openpyxl
  if errorlevel 1 (
    echo.
    echo   Install failed. If you are behind a company proxy, pip may be blocked.
    echo   Ask IT to allow pypi.org, or ask them to install these two packages.
    echo.
    pause
    exit /b 1
  )
)

rem --- go ------------------------------------------------------------------
%PY% pump_app.py
if errorlevel 1 (
  echo.
  echo   The app exited with an error. The message above is the useful bit.
  echo.
  pause
)
