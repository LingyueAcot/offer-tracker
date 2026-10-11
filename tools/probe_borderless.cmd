@echo off
chcp 65001 >nul
title borderless window probe
cd /d "%~dp0"
rem ---------------------------------------------------------------
rem Double-click this file. It opens a borderless test window and
rem walks you through 4 things to try by hand.
rem Keep this file ASCII-only (non-ASCII breaks cmd.exe parsing).
rem ---------------------------------------------------------------

set "PY312=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PY312%" (
  "%PY312%" probe_borderless.py %*
  goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
  python probe_borderless.py %*
  goto :done
)

py probe_borderless.py %*

:done
echo.
echo Finished. Output is also saved to work\borderless_probe.txt
echo Press any key to close this window...
pause >nul
