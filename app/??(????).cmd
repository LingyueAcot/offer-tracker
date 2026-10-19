@echo off
chcp 65001 >nul
title Resume Tracker - Debug
cd /d "%~dp0"

rem 平时双击桌面上的「应届海投管理」图标就行（不会出现黑框）。
rem 这个文件是出问题时用的：它会保留命令行输出，方便看报错。
rem 日志也会写在 logs\launcher.log。

echo Starting Resume Tracker (debug mode)...
echo.

set PY312=%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe
if exist "%PY312%" (
  "%PY312%" launcher.py
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python launcher.py
  ) else (
    py launcher.py
  )
)

echo.
echo Done. Press any key to close this window.
pause >nul
