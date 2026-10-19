@echo off
chcp 65001 >nul
title pywebview interface probe
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem Double-click this file. It runs probe_pywebview.py with the local
rem Python 3.12, which:
rem   1. prints the real pywebview API into work\pywebview_api.txt
rem   2. opens a real v3 window so you can see it working
rem Close that window and this script finishes.
rem
rem Add --api-only to skip the window.
rem
rem NOTE: keep this file ASCII-only. Non-ASCII characters break
rem cmd.exe parsing here (the rem lines get split). All Chinese text
rem lives in probe_pywebview.py instead.
rem ---------------------------------------------------------------

set "PY312=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PY312%" (
  "%PY312%" probe_pywebview.py %*
  goto :done
)

:try_path_python
where python >nul 2>nul
if %errorlevel%==0 (
  python probe_pywebview.py %*
  goto :done
)

py probe_pywebview.py %*

:done
echo.
echo Finished. Output is also saved to work\pywebview_api.txt
echo Press any key to close this window...
pause >nul
