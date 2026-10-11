@echo off
chcp 65001 >nul
title window frame measure
cd /d "%~dp0"
rem Keep this file ASCII-only (non-ASCII breaks cmd.exe parsing).
rem Step 1: open the app. Step 2: double-click this file.

set "PY312=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PY312%" (
  "%PY312%" measure_window_frame.py
  goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
  python measure_window_frame.py
  goto :done
)

py measure_window_frame.py

:done
echo.
echo Result saved to work\window_frame.txt
echo Press any key to close this window...
pause >nul
