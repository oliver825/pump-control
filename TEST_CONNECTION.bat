@echo off
cd /d "%~dp0"
title Pump connection test

set PY=
where py >nul 2>nul && set PY=py
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
  echo   Python is not installed. Run RUN_ME.bat first for instructions.
  pause
  exit /b 1
)

%PY% -c "import serial" >nul 2>nul
if errorlevel 1 %PY% -m pip install --user pyserial openpyxl

echo.
echo   ============================================================
echo    BEFORE YOU START: press MODE on the pump until the display
echo    shows "dig". The serial port does nothing until it does.
echo   ============================================================
echo.

%PY% test_connection.py

echo.
set /p PORT=  Type the COM port to test (e.g. COM4), or just press Enter to quit: 
if "%PORT%"=="" exit /b 0

%PY% test_connection.py %PORT%

echo.
pause
