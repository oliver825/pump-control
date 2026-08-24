@echo off
REM ---------------------------------------------------------------------
REM  Builds Pump Control.exe so the app runs on machines with no Python.
REM  Run this from the project folder. Output lands in dist\
REM
REM  Must be run ON WINDOWS. PyInstaller does not cross-compile: building
REM  on a Mac gives you a Mac app, not a Windows .exe.
REM ---------------------------------------------------------------------
setlocal

echo Installing PyInstaller if needed...
python -m pip install --quiet pyinstaller pyserial openpyxl
if errorlevel 1 goto :fail

echo.
echo Building. This takes a minute or two.
python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name PumpControl ^
    --hidden-import serial.tools.list_ports_windows ^
    --clean ^
    --noconfirm ^
    pump_app.py
if errorlevel 1 goto :fail

echo.
echo ==========================================================
echo  Done. Your app is:   dist\PumpControl.exe
echo.
echo  Test it before sharing it. Reports are written to a
echo  "logs" folder next to the .exe, so put the .exe
echo  somewhere writable, not in Program Files.
echo ==========================================================
pause
exit /b 0

:fail
echo.
echo Build failed. Most common causes:
echo   - Python 3.14 is very new and PyInstaller may not support it yet.
echo     Install Python 3.12 or 3.13 and build with that instead.
echo   - Antivirus blocked the build. Try again with it paused.
pause
exit /b 1
