@echo off
chcp 65001 >nul
title Resume Tracker - Safe Mode
cd /d "%~dp0"
rem ---------------------------------------------------------------
rem Use this when the normal window is broken (no title bar, cannot
rem drag, black bar, blank window ...).
rem It ignores config.json and opens the app with the normal Windows
rem title bar, which always works. Your data is not touched.
rem Keep this file ASCII-only: non-ASCII breaks cmd.exe parsing.
rem ---------------------------------------------------------------

echo Starting in SAFE MODE (normal Windows title bar)...
echo.

set "PY312=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PY312%" (
  "%PY312%" launcher.py --safe-mode
  goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
  python launcher.py --safe-mode
  goto :done
)

py launcher.py --safe-mode

:done
echo.
echo Done. Press any key to close this window...
pause >nul
