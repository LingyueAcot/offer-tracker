# -*- coding: utf-8 -*-
r"""打一个「给同学试用的空白包」：只有程序，没有任何用户数据。

【为什么单独一个脚本】原来那套发布工具早停维护了，而且它面向的是
"带数据分享"。这里要的是**全新的空白程序**：对方双击就能开始记自己的投递。

【两条不能破】
  1. **绝不带用户数据**：data/、logs/、webview_profile/、个人画像.json 一个都不进包；
     连项目文档、接口说明这类内部文档也不进（对方不需要，而且里面有这台电脑路径）。
  2. 对方机器上只要有 Python 3.10+ 和 WebView2，「首次使用.cmd」会自己 pip install pywebview。

用法：python tools\make_trial_package.py
产出：试用包\应届海投管理-试用版\  +  试用包\应届海投管理-试用版.zip
"""
from __future__ import print_function

import io
import os
import shutil
import sys
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
OUT_ROOT = os.path.join(ROOT, u"试用包")
PKG_NAME = u"应届海投管理-试用版"
PKG = os.path.join(OUT_ROOT, PKG_NAME)

FILES = [
    "launcher.py", "server.py", "schema.py", "backup.py", "store.py", "winenv.py",
    "requirements.txt",
    u"导入模板.xlsx", u"AI转换提示词.md", u"模板说明.md", u"使用说明.md",
]
DIRS = ["core", "api", "services", "web", "wheels"]

# 「必须带走的」清单：FILES/DIRS 漏写一项，程序在别人机器上会直接崩，
# 而漏写本身是**没有报错**的（2026-09-24 store.py + services/ 就这么漏过一次，
# 差点发出一个装上去就起不来的升级包）。所以这里独立写一份最低要求来兜底：
# 漏了、或者忘了同步更新这里，打包就报错退出。
REQUIRED = [
    "launcher.py", "server.py", "schema.py", "store.py", "backup.py", "winenv.py",
    "core/__init__.py", "core/excel.py", "api/__init__.py",
    "services/__init__.py", "services/apps.py", "services/talks.py",
    "services/state.py", "web/index.html", "启动.cmd",
    # 离线依赖包（2026-09-25 加）：缺了首次启动会退回联网装 pywebview，
    # 在连不通的机器上就是"卡住 + 一堆红色报错"。
    # 以 `*` 结尾 = 前缀匹配（文件名带版本号，不写死；见下面 gone 的算法）。
    "wheels/pywebview-*",
]

CMD = r"""@echo off
rem ============================================================
rem  Resume Tracker - first run helper.
rem  Keep this file ASCII only: Chinese text can break cmd.exe
rem  parsing (the rem lines get cut in half).
rem ============================================================
setlocal
cd /d "%~dp0"
title Resume Tracker - setup

echo.
echo  Resume Tracker (trial) - first run
echo  ------------------------------------------------

rem ---- [1/3] find a Python 3.10+, or install one automatically ----
rem  PY always holds a ready-to-run command line (either "py -3" or a QUOTED full path),
rem  FOUND is a separate flag so the emptiness test never trips over quoting.
set PY=
set FOUND=
py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set PY=py -3 && set FOUND=1
if not "%FOUND%"=="1" python -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set PY=python && set FOUND=1
if not "%FOUND%"=="1" python3 -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set PY=python3 && set FOUND=1
if "%FOUND%"=="1" goto havepython

echo  [1/3] Python 3.10+ not found - installing it for you ...
echo        about 25 MB download, installs for the CURRENT USER only ^(no admin prompt^).
where winget >nul 2>nul
if not errorlevel 1 (
  echo        using winget ... ^(this can take a minute^)
  rem Prefer the 64-bit build (2026-09-27): a tester ended up with Python312-32,
  rem and 32-bit Python lacks some user32 exports the window shell needs - fixed in
  rem the program too, but 64-bit simply has fewer edges. If this manifest does not
  rem accept --architecture, fall back to the plain command so we never install LESS.
  winget install -e --id Python.Python.3.12 --scope user --architecture x64 --silent --accept-source-agreements --accept-package-agreements --disable-interactivity
  if errorlevel 1 winget install -e --id Python.Python.3.12 --scope user --silent --accept-source-agreements --accept-package-agreements --disable-interactivity
  goto afterinstall
)
echo        winget not available, downloading the official installer ...
rem ---- TIMEOUTS ARE MANDATORY HERE (2026-09-25) ----
rem  Without --connect-timeout/--max-time this curl hangs FOREVER on a machine that
rem  cannot reach python.org, and --silent means the user sees nothing at all - the
rem  report was literally "stuck on step 1 for ages with no download". A tester hit it.
rem  Now: give up after 10s of not connecting / 5 min total, and SAY what is happening.
echo        downloading from python.org ... ^(if this stalls, it moves on by itself^)
set INSTALLER=%TEMP%\python-3.12.8-amd64.exe
curl -L --fail --show-error --connect-timeout 10 --max-time 300 --retry 1 -o "%INSTALLER%" https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
if errorlevel 1 (
  echo        python.org did not work, trying a China mirror ...
  curl -L --fail --show-error --connect-timeout 10 --max-time 300 --retry 1 -o "%INSTALLER%" https://mirrors.huaweicloud.com/python/3.12.8/python-3.12.8-amd64.exe
)
if errorlevel 1 goto nopython
echo        installing silently ^(per-user, adds to PATH, no admin^) ...
"%INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_test=0 Include_launcher=1 SimpleInstall=1
if errorlevel 1 goto nopython

:afterinstall
rem PATH in this window is stale right after installing, so look where it actually lands
for %%V in (313 312 311 310) do if not "%FOUND%"=="1" if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" && set FOUND=1
if not "%FOUND%"=="1" if exist "%ProgramFiles%\Python312\python.exe" set PY="%ProgramFiles%\Python312\python.exe" && set FOUND=1
if not "%FOUND%"=="1" if exist "%ProgramFiles%\Python311\python.exe" set PY="%ProgramFiles%\Python311\python.exe" && set FOUND=1
rem the interpreter we just installed is known by FULL PATH - take its sibling pythonw.exe
if "%FOUND%"=="1" set PYW=%PY:python.exe=pythonw.exe%
if "%FOUND%"=="1" echo        installed OK.
if "%FOUND%"=="1" goto havepython
goto nopython

:havepython
echo  [1/3] Python: %PY%
%PY% -c "import sys;print('        version ' + sys.version.split()[0])"

rem Work out the WINDOWED launcher (pythonw) from whatever we found.
rem Why this matters A LOT: a console interpreter (python.exe) gets a black console window,
rem and CLOSING that black window kills the whole process group - the app exits with it.
rem The first tester saw exactly that ("black window + app, closing the black one quits").
rem pythonw.exe has no console, so there is nothing to close.
rem   py -3            -> pyw -3            (the py launcher ships a windowed twin)
rem   python / python3 -> pythonw          (same folder, already on PATH)
rem   full path        -> its sibling pythonw.exe (set in :afterinstall)
rem Every candidate is existence-checked. If none works we do NOT fall back to a console
rem python - we start it through wscript with a hidden window instead (see :hiddenstart).
rem Ask the interpreter itself where it lives, then take ITS OWN sibling pythonw.exe.
rem Two things this buys us: (a) the windowed launcher is EXACTLY the same Python that we
rem just checked pywebview on - a different one might not have pywebview at all;
rem (b) it also covers "py -3", whose pyw.exe is often not on PATH (measured: where pyw
rem finds nothing on a normal machine, so guessing by name would fall through needlessly).
set PYEXE=
for /f "delims=" %%I in ('%PY% -c "import sys;print(sys.executable)"') do set PYEXE=%%I
if not defined PYEXE goto pywfallback
set PYW="%PYEXE:python.exe=pythonw.exe%"
if exist %PYW% goto pywdone
set PYW=

:pywfallback
rem Rare: no sibling pythonw.exe. The py launcher ships its own windowed twin; try that.
if /i "%PY%"=="py -3" (
  where pyw >nul 2>nul && set PYW=pyw -3
)
if "%PYW%"=="" set PYW=HIDDEN

:pywdone
echo         launching with: %PYW%

rem ---- [2/3] dependencies: only touch pip when something is actually missing ----
rem  Why the import test first: "pip install X" on an already-installed X still asks the
rem  index every time, and with no internet it FAILS - which would block a perfectly
rem  fine install. So: import them; only if that fails do we go online.
echo  [2/3] checking pywebview / openpyxl ...
%PY% -c "import webview, openpyxl" >nul 2>nul
if not errorlevel 1 (
  echo        already installed - skipping pip ^(no internet needed^).
  goto webview2
)
rem ---- OFFLINE FIRST (2026-09-25) ----
rem  The wheels/ folder next to this file holds every dependency, so a fresh machine
rem  needs NO internet for this step. Before this existed, first run went straight to
rem  pip over the network - which on a slow/blocked connection hung for minutes and
rem  then printed scary red pip errors (a tester hit exactly that).
rem  Only when the local copy cannot satisfy the install do we go online.
echo        installing from the bundled offline copy ^(no internet needed^) ...
%PY% -m pip --version >nul 2>nul
if errorlevel 1 (
  echo        pip is missing - bootstrapping it ...
  %PY% -m ensurepip --upgrade >nul 2>nul
)
if not exist "%~dp0wheels" goto localfail
%PY% -m pip install --user --quiet --disable-pip-version-check --no-index --find-links "%~dp0wheels" pywebview openpyxl
if not errorlevel 1 (
  echo        done ^(from the offline copy^).
  goto webview2
)
:localfail
echo        offline copy did not fit this Python - fetching them ^(needs internet^) ...
set DEPSOK=
call :trydeps "https://pypi.tuna.tsinghua.edu.cn/simple"
if "%DEPSOK%"=="1" goto webview2
call :trydeps "https://mirrors.aliyun.com/pypi/simple"
if "%DEPSOK%"=="1" goto webview2
call :trydeps "https://pypi.org/simple"
if "%DEPSOK%"=="1" goto webview2
goto pipfail

:trydeps
echo        trying %~1 ...
%PY% -m pip install --user --quiet --disable-pip-version-check -i %1 pywebview openpyxl
if not errorlevel 1 set DEPSOK=1
goto :eof

rem ---- [3/3] check the WebView2 runtime (the window cannot render without it) ----
:webview2
echo  [3/3] checking WebView2 runtime ...
call :haswebview2
if "%WV2%"=="1" (
  echo        already installed.
  goto startapp
)
echo        not found - installing it for you ^(about 2 MB^) ...
set WV2SETUP=%TEMP%\MicrosoftEdgeWebview2Setup.exe
curl -L --fail --silent --show-error -o "%WV2SETUP%" "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
if errorlevel 1 (
  echo        download failed.
) else (
  echo        running the installer silently ...
  "%WV2SETUP%" /silent /install
)
call :haswebview2
if "%WV2%"=="1" (
  echo        installed OK.
  goto startapp
)
echo.
echo  [!] WebView2 could not be installed automatically ^(no internet, or blocked^).
echo      Install it by hand from this link, then run this file again:
echo        https://go.microsoft.com/fwlink/p/?LinkId=2124703
echo.
pause
goto startapp

:haswebview2
set WV2=
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" >nul 2>nul && set WV2=1
if "%WV2%"=="1" goto :eof
reg query "HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" >nul 2>nul && set WV2=1
if "%WV2%"=="1" goto :eof
reg query "HKCU\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" >nul 2>nul && set WV2=1
goto :eof

:startapp
echo  starting ... a window should appear in a few seconds.
if not exist "%~dp0launcher.py" (
  echo  [!] launcher.py is missing next to this file - extract the whole zip again.
  pause
  exit /b 1
)
rem One plain-ASCII line of evidence: if a tester reports a problem, this file says which
rem interpreter was picked. Safe for them to send back (it holds no personal data).
rem 2026-09-27: the bitness is recorded too - 32-bit Python was the root cause of
rem "window cannot be dragged / buttons do nothing" on one machine.
set RTBITS=
for /f "delims=" %%B in ('%PY% -c "import struct;print(struct.calcsize('P')*8)" 2^>nul') do set RTBITS=%%B
echo %DATE% %TIME%  PY=%PY%  PYW=%PYW%  BITS=%RTBITS%>>"%~dp0startup-log.txt"
if "%PYW%"=="HIDDEN" goto hiddenstart
start "" %PYW% "%~dp0launcher.py"
exit /b 0

:hiddenstart
rem Last resort: no pythonw.exe on this machine. wscript starts the console python with its
rem window HIDDEN (mode 0), so the tester still has no black window to close by accident.
echo         no pythonw.exe found - starting through a hidden-window helper ...
wscript //nologo "%~dp0_hidden_start.vbs" %~PY% "%~dp0launcher.py"
exit /b 0

:nopython
echo.
echo  [!] Automatic install did not work ^(no internet? blocked?^).
echo      1. Download Python 3.10+ from https://www.python.org/downloads/
echo      2. During setup, TICK "Add python.exe to PATH"
echo      3. Then double-click this file again.
echo.
pause
exit /b 1

:pipfail
echo.
echo  [!] Installing pywebview failed (no internet?).
echo      Try again later, or run this by hand in a terminal:
echo        %PY% -m pip install --user pywebview openpyxl
echo.
pause
exit /b 1
"""

README = u"""# 应届海投管理 · 试用版

一个**纯本地**的求职进度管理小工具：记投递到哪一步、看宣讲会行程、随手记备忘。
数据全部存在你自己电脑上的一个 SQLite 文件里，**不联网、不上传**。
（只有「同步宣讲会」会去抓学校就业网；你学校没这个页面就别点它。）

## 一、怎么开始（Windows）

1. 把整个文件夹解压到任意位置（别放在只读目录里）；
2. 双击 **启动.cmd**（每次都用它启动）：
   - 它先找你的 Python（需要 3.10 以上，没有会告诉你去哪装）；
   - 再检查依赖：**装好了就直接跳过**（不会重复安装、也不用联网），缺了才去装；
   - 再查 WebView2 运行时在不在，缺了会告诉你装哪个；
   - 然后弹出程序窗口。
   - 第一次启动成功后，**桌面上会自动出现一个「应届海投管理」快捷方式**，以后双击它就行
     （它只是替你跑同一个 launcher.py，不会另外复制一份程序）。**已经有了就不会动你的**：
     你自己改过图标或名字都保留。
3. 万一自绘窗口出问题（白屏 / 拖不动）：双击同目录的 **启动（安全模式）.cmd**（带系统标题栏），
   它是保底入口。

> 需要 **WebView2 运行时**：Windows 11 自带；Windows 10 一般随 Edge 装好了。
> 万一提示缺它，装微软官方的 WebView2 Runtime 就行。

## 二、第一次用，数据怎么进去（两条路）

**路 A：已经有自己的 Excel**（推荐）

1. 程序里点右上角「导出」→ **下载导入模板.xlsx**；
2. 再点 **复制给 AI 的转换提示词**；
3. 把「你的 Excel + 模板 + 那段提示词」三个一起发给 AI（外部评审 / DeepSeek 都行）；
4. AI 给你一个新的 xlsx，回到程序点「导出 → 导入 Excel」选它 —— 完事。

**路 B：从零开始**：点右上角「＋ 新增投递」，一天记一条也不累。

## 三、你的数据在哪

- 数据：data 文件夹里的 applications.db（一个 SQLite 文件，拷走它就是拷走全部）；
- 每次启动会自动备份到 data\\backups\\时间戳\\，**不覆盖、不自动清理**；
- 想搬走或备份：直接复制整个 data 文件夹。

## 四、常见问题

| 现象 | 怎么办 |
| --- | --- |
| 双击后没反应 | 跑 启动.cmd 会留一个命令行窗口，提示都在里面；多半是 Python 没装或没勾 PATH |
| 窗口一片白 | 关掉重开；还不行就用 启动（安全模式）.cmd |
| 提示缺 WebView2 | 装微软的 WebView2 Runtime（免费） |
| 想看报错 | 用 启动.cmd 启动会留一个命令行窗口，报错都在里面 |
| 桌面上没有快捷方式 | 启动成功后它会自己建一个；没建出来多半是系统禁了 PowerShell（不影响用，双击 启动.cmd 一样跑）。日志在 logs\\launcher.log 里搜「快捷方式」 |

## 五、这是什么版本

自己用工具，功能到「够自己用」为止：投递进度、公司分组、看板、宣讲会、备忘录、
导出 JSON / CSV、Excel 导入、AI 辅助转换、调研任务包、宣讲会自动同步。
有问题直接跟我说，我改。
"""


VBS = r"""' Starts the app with a HIDDEN window - used only when no pythonw.exe exists.
' A console python would otherwise show a black window, and closing that window kills the
' app; this way there is no window to close. Args: <exe or command> "<launcher.py>".
Option Explicit
Dim sh, fso, args, cmd, i, Q
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set args = WScript.Arguments
If args.Count < 2 Then WScript.Quit 2
Q = Chr(34)
cmd = ""
For i = 0 To args.Count - 1
  cmd = cmd & Q & args(i) & Q & " "
Next
sh.CurrentDirectory = fso.GetParentFolderName(args(args.Count - 1))
sh.Run cmd, 0, False
"""

SAFE_CMD = r"""@echo off
rem ============================================================
rem  Resume Tracker - SAFE MODE (the escape hatch).
rem  Keep this file ASCII only: Chinese text breaks cmd.exe parsing.
rem  Use it when the normal window is broken (no title bar, cannot
rem  drag, black bar, blank page). It ignores config.json and opens
rem  the app with the normal Windows title bar. Data is not touched.
rem  It opens the app WINDOWED on purpose: a console window would
rem  kill the app when closed (the first tester closed it by hand).
rem ============================================================
setlocal
cd /d "%~dp0"
title Resume Tracker - Safe Mode

set PYW=
for %%V in (313 312 311 310) do if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe" set PYW="%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe"
if not defined PYW if exist "%ProgramFiles%\Python312\pythonw.exe" set PYW="%ProgramFiles%\Python312\pythonw.exe"
if not defined PYW (
  where pythonw >nul 2>nul && set PYW=pythonw
)
if not defined PYW (
  echo  [!] No pythonw.exe found. Run the normal setup file once - it installs everything.
  pause
  exit /b 1
)

echo  Opening in SAFE MODE (normal Windows title bar) ...
start "" %PYW% "%~dp0launcher.py" --safe-mode
timeout /t 4 >nul
if exist "%~dp0logs\launcher.log" (
  echo.
  echo  ---- last 12 lines of logs\launcher.log ----
  powershell -NoProfile -Command "Get-Content -LiteralPath '%~dp0logs\launcher.log' -Tail 12"
)
echo.
echo  Done. If a window opened, you may close THIS window freely.
pause >nul
"""


UPGRADE_NAME = u"应届海投管理-升级包"

UPGRADE_CMD = r"""@echo off
rem ============================================================
rem  Resume Tracker - in-place upgrade. YOUR DATA IS KEPT.
rem
rem  HOW: extract this folder INTO the program folder (the one
rem  that contains launcher.py and data\), then double-click
rem  this file. It only replaces program files.
rem  Keep this file ASCII only: Chinese text breaks cmd.exe.
rem ============================================================
setlocal
cd /d "%~dp0"
title Resume Tracker - upgrade

rem Two layouts are accepted:
rem   (a) the whole extracted folder sits INSIDE the program folder -> target is ".."
rem   (b) the files were extracted straight INTO the program folder -> target is "."
rem (b) is what most people do, and it needs no copying: the extraction already
rem replaced the files. The first version only accepted (a) and refused (b).
rem Order matters: a payload copy ALWAYS contains launcher.py, so testing ".\launcher.py"
rem first would swallow layout (a). Check the parent first, then fall back to "this is it".
set TARGET=..
if not exist "..\launcher.py" if exist ".\launcher.py" set TARGET=.
if not exist "%TARGET%\launcher.py" goto wrongplace
if not exist "%TARGET%\server.py" goto wrongplace
rem "this is it" is only believable if the folder has been used before (data\ or logs\)
if "%TARGET%"=="." if not exist "data" if not exist "logs" goto notsure

set BAK=_before_upgrade
if exist "%BAK%" set BAK=_before_upgrade_2
if exist "%BAK%" set BAK=_before_upgrade_3

echo  Program folder : %CD%\%TARGET%
echo  Your data (data\ logs\ config.json) is NOT touched.
echo.

if "%TARGET%"=="." goto alreadyinplace

echo  [1/3] backing up the current program files ...
if not exist "%BAK%" mkdir "%BAK%"
for %%F in (*.*) do if exist "..\%%F" copy /Y "..\%%F" "%BAK%\" >nul
rem The folder list is deliberately NOT hard-coded: whatever subfolders the
rem payload has, get backed up. Hard-coding (core api web) bit us on 2026-09-24 --
rem the newly added services\ was skipped, the old install never got it, and the
rem program would not start while the check still said "done".
for /d %%D in (*) do (
  if /i not "%%D"=="%BAK%" (
    if exist "..\%%D" (
      if not exist "%BAK%\%%D" mkdir "%BAK%\%%D"
      xcopy /E /I /Y /Q "..\%%D" "%BAK%\%%D" >nul
    )
  )
)

echo  [2/3] copying the new version in ...
rem config.json and startup-log.txt belong to the USER - never overwrite them.
rem (config.json ships in the package for a fresh install; on an upgrade it must be kept.)
for %%F in (*.*) do if /i not "%%F"=="config.json" if /i not "%%F"=="startup-log.txt" copy /Y "%%F" "..\" >nul
for /d %%D in (*) do (
  if /i not "%%D"=="%BAK%" xcopy /E /I /Y /Q "%%D" "..\%%D" >nul
)

goto verify

:alreadyinplace
echo  This IS the program folder (launcher.py is right here), so the new files
echo  were already put in place when you extracted - nothing to copy, just checking.
echo.
:verify
echo  [3/3] checking ...
rem Name the files we actually need instead of only testing launcher.py + core\excel.py:
rem those two can be present while the whole services\ folder is missing (seen on
rem 2026-09-24), and then the user reads "done" but the program will not open.
rem 2026-09-27: winenv.py / schema.py / backup.py are named too - whenever a new
rem top-level module is added it MUST be added here as well. That day the packaging
rem list was missing winenv.py: the upgrade "succeeded", then the program could not
rem even start (import error).
for %%F in (launcher.py server.py schema.py backup.py store.py winenv.py core\excel.py core\__init__.py api\__init__.py services\__init__.py services\apps.py services\talks.py services\state.py web\index.html) do (
  if not exist "%TARGET%\%%F" (
    echo  [!] missing: %%F
    goto failed
  )
)
echo.
echo  Done.
if exist "..\data\applications.db" (echo    data\applications.db - kept, byte for byte) else (echo    no data\ yet - a fresh one is created on first run)
echo    logs\ and config.json - kept as they were
echo.
echo  Now close this window and double-click the normal startup file.
if not "%TARGET%"=="." echo  If anything looks wrong, copy %BAK%\ back over the program folder.
if "%TARGET%"=="." echo  Heads-up: extracting straight into this folder also replaced config.json
if "%TARGET%"=="." echo  with the package copy. Your data\ and logs\ were NOT touched.
pause >nul
exit /b 0

:notsure
echo.
echo  [!] This folder has launcher.py but no data\ and no logs\, so it looks like a
echo      fresh copy rather than the program folder you have been using.
echo      If you DID extract these files into your program folder, then you are done.
echo      Otherwise extract them into the folder that has launcher.py AND data\.
echo.
pause >nul
exit /b 1

:wrongplace
echo.
echo  [!] launcher.py is not here and not one level up, so this does not look
echo      like the program folder. Do one of these:
echo        1. extract the WHOLE folder INTO the program folder (the one that has
echo           launcher.py and data\) and run this file from inside it; or
echo        2. extract the files straight INTO that program folder and run this file there.
echo.
pause >nul
exit /b 1

:failed
echo.
echo  [!] Something did not copy. The old files are in %BAK%\ - copy them back.
pause >nul
exit /b 1
"""


def build_upgrade(pkg):
    """由试用包内容生成升级包：同一批程序文件 + 一个原地升级脚本（绝不碰数据）。"""
    up = os.path.join(OUT_ROOT, UPGRADE_NAME)
    # 升级.cmd 必须以 ASCII 写出：cmd.exe 在 GBK 代码页下读 UTF-8 中文会把 rem 行
    # 切成半截，后面的命令跟着遭殃（.cmd.header 顶上就写着这条）。这里提前拦，
    # 免得改了 UPGRADE_CMD 的人等到打包最后一步才吃一个 UnicodeEncodeError。
    try:
        UPGRADE_CMD.encode("ascii")
    except UnicodeEncodeError as exc:
        bad = UPGRADE_CMD[exc.start:exc.end]
        raise SystemExit(u"UPGRADE_CMD 里混进了非 ASCII 字符 %r —— 升级.cmd 只能是英文"
                         % bad)
    if os.path.isdir(up):
        shutil.rmtree(up)
    shutil.copytree(pkg, up)
    with io.open(os.path.join(up, u"升级.cmd"), "w", encoding="ascii", newline="\r\n") as h:
        h.write(UPGRADE_CMD)
    zpath = os.path.join(OUT_ROOT, UPGRADE_NAME + u".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _dirs, files in os.walk(up):
            for name in files:
                full = os.path.join(dirpath, name)
                z.write(full, os.path.join(UPGRADE_NAME, os.path.relpath(full, up)))
    print(u"升级包：%s（%.2f MB）" % (zpath, os.path.getsize(zpath) / 1024.0 / 1024.0))
    return 0


def build_oneshot(pkg):
    """
    一键升级：把这批程序文件 + 升级用的 PowerShell 脚本打成**一个 .cmd 文件**。

    做法是「批处理头 + zip 负载」拼在同一个文件里：cmd.exe 执行到最后的 exit /b 就停了，
    后面的二进制它一个字都不会读（自解压包的经典做法）。批处理先把整个文件复制成 %TEMP%
    下的 .zip，再用 tar（Win10 1803+ 自带）或 PowerShell 解开。
    用户只需要双击这一个文件：不用解压、不用放到哪里、不用选目录。
    """
    header = io.open(os.path.join(ROOT, "tools", u"一键升级.cmd.header"), "rb").read()
    # cmd.exe 只认 CRLF：LF 换行会让它把后面的二进制当成命令行继续读（实测踩到）
    header = header.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    ps1 = io.open(os.path.join(ROOT, "tools", u"one-click-upgrade.ps1"), "rb").read()
    # Windows PowerShell 5.1 没有 BOM 就按 ANSI 读脚本，中文全成乱码（实测把语法都读崩了）
    if not ps1.startswith(b"\xef\xbb\xbf"):
        ps1 = b"\xef\xbb\xbf" + ps1
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _dirs, files in os.walk(pkg):
            for name in files:
                if name == u"给试用的人看.md":
                    continue
                full = os.path.join(dirpath, name)
                z.write(full, os.path.relpath(full, pkg))
        z.writestr("one-click-upgrade.ps1", ps1)
    out = os.path.join(OUT_ROOT, u"一键升级.cmd")
    with io.open(out, "wb") as h:
        h.write(header)
        h.write(buf.getvalue())
    print(u"一键升级：%s（%.2f MB，双击这一个文件就行）" % (out, os.path.getsize(out) / 1024.0 / 1024.0))
    return 0


def main():
    if os.path.isdir(OUT_ROOT):
        shutil.rmtree(OUT_ROOT)
    os.makedirs(PKG)

    copied, missing = [], []
    for name in FILES:
        src = os.path.join(APP, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(PKG, name))
            copied.append(name)
        else:
            missing.append(name)
    for name in DIRS:
        src = os.path.join(APP, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(PKG, name),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.before_*"))
            copied.append(name + "/")
        else:
            missing.append(name)

    # 正式版的 config.json —— 注意 `"build": "release"` 这一行：
    # 它让程序在界面上显示「正式版」（见 server.py 的 detect_edition）。
    # **只有这里**（发布包）会写它；用户自己那份 config.json 里没有这个键，
    # 也不该有 —— 升级时 config.json 是保留用户那份的。
    # 另外：这里**不写** version 字段。版本号的唯一来源是 server.py 的 APP_VERSION，
    # 在 config.json 里再写一份就会出现"两份版本号不一致"（以前 config.json 里那个
    # "version": "V1" 就是这么来的，已经清掉）。
    with io.open(os.path.join(PKG, "config.json"), "w", encoding="utf-8", newline="\n") as h:
        h.write(u'{\n  "port": 8768,\n  "edition": "desktop-v3-trial",\n  "build": "release",\n'
                u'  "desktop": true,\n'
                u'  "python": "",\n  "shell": "pywebview",\n  "window_mode": "borderless",\n'
                u'  "relaunch_windowed": true,\n  "talk_sync_days": 30\n}\n')
    with io.open(os.path.join(PKG, u"启动.cmd"), "w", encoding="ascii", newline="\r\n") as h:
        h.write(CMD)
    with io.open(os.path.join(PKG, u"给试用的人看.md"), "w", encoding="utf-8", newline="\n") as h:
        h.write(README)
    with io.open(os.path.join(PKG, u"_hidden_start.vbs"), "w", encoding="ascii", newline="\r\n") as h:
        h.write(VBS)
    with io.open(os.path.join(PKG, u"启动（安全模式）.cmd"), "w", encoding="ascii", newline="\r\n") as h:
        h.write(SAFE_CMD)

    bad = []
    for dirpath, _dirs, files in os.walk(PKG):
        parts = dirpath.split(os.sep)
        for name in files:
            if name.lower() in (u"个人画像.json", "applications.db", "change_log.jsonl",
                               u"开发记录", u"数据接口说明.md"):
                bad.append(os.path.join(dirpath, name) + u"（不该出现的文件）")
            if "backups" in parts or "logs" in parts:
                bad.append(os.path.join(dirpath, name) + u"（在备份/日志目录里）")
            if not name.lower().endswith((".md", ".cmd", ".json", ".py", ".js", ".html", ".txt")):
                continue
            try:
                text = io.open(os.path.join(dirpath, name), encoding="utf-8", errors="ignore").read()
            except IOError:
                continue
            for needle in (u"张小明", u"E:" + os.sep + "AI work"):
                if needle in text:
                    bad.append(name + u"（内容里出现 " + needle + u"）")

    size = sum(os.path.getsize(os.path.join(d, f)) for d, _s, fs in os.walk(PKG) for f in fs)
    print(u"打包目录：%s" % PKG)
    print(u"带走 %d 项：%s" % (len(copied), u"、".join(copied)))
    if missing:
        print(u"⚠️ 没找到：%s" % u"、".join(missing))
        bad.append(u"源文件不存在：" + u"、".join(missing))
    # ---- 「漏文件」的硬兜底：对着 app/ 目录自己算，不靠手写清单（2026-09-27 加）----
    # 为什么加：上面那份 `REQUIRED` 是**手写**的，2026-09-27 新增 `winenv.py` 时它没跟着更新，
    # 于是打出来的包**少了这个文件** —— 打包器自己的检查照样全过，装到别人机器上
    # `import winenv` 直接起不来（是 `升级自检` 真升一遍才抓到的）。
    # 这条改成**从源目录反推**：app/ 下每个 .py、每个含 .py 的子目录，都必须出现在
    # FILES / DIRS 里；漏一个就 exit 1。以后再新增模块，忘了改清单会当场报错。
    derived_missing = []
    for name in sorted(os.listdir(APP)):
        path = os.path.join(APP, name)
        if os.path.isfile(path) and name.endswith(".py") and name not in FILES:
            derived_missing.append(name)
        elif os.path.isdir(path) and name not in DIRS:
            try:
                if any(f.endswith(".py") for f in os.listdir(path)):
                    derived_missing.append(name + u"/")
            except OSError:
                pass
    if derived_missing:
        print(u"❌ app/ 里有程序文件没进打包清单：%s" % u"、".join(derived_missing))
        print(u"   （改脚本顶部的 FILES / DIRS —— 漏了它们，别人装上去会直接起不来）")
        bad.append(u"打包清单漏项：" + u"、".join(derived_missing))
    else:
        print(u"漏文件兜底自查：✅ app/ 下的程序文件/目录都进清单了")
    # 必需文件自查：`REQUIRED` 里以 `*` 结尾的当**前缀**匹配（用于 wheels/ —— 那些文件名
    # 带版本号，写死版本号的话升级依赖时会误报"必需文件没进包"）。
    gone = []
    for p in REQUIRED:
        full = os.path.join(PKG, p.replace("/", os.sep))
        if p.endswith("*"):
            parent_dir = os.path.dirname(full)
            prefix = os.path.basename(full)[:-1]
            ok = os.path.isdir(parent_dir) and any(
                n.startswith(prefix) for n in os.listdir(parent_dir))
        else:
            ok = os.path.exists(full)
        if not ok:
            gone.append(p)
    if gone:
        print(u"❌ 必需文件没进包：%s（改这里：脚本顶部的 FILES / DIRS）" % u"、".join(gone))
        bad.append(u"必需文件没进包：" + u"、".join(gone))
    else:
        print(u"必需文件自查：✅ %d 项都在" % len(REQUIRED))
    print(u"体积：%.2f MB" % (size / 1024.0 / 1024.0))
    print(u"隐私自查：" + (u"✅ 没有用户数据、也没有这台电脑路径" if not bad else u"❌ " + u"；".join(bad)))

    zpath = os.path.join(OUT_ROOT, PKG_NAME + u".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _dirs, files in os.walk(PKG):
            for name in files:
                full = os.path.join(dirpath, name)
                z.write(full, os.path.join(PKG_NAME, os.path.relpath(full, PKG)))
    print(u"压缩包：%s（%.2f MB）" % (zpath, os.path.getsize(zpath) / 1024.0 / 1024.0))
    build_upgrade(PKG)
    build_oneshot(PKG)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
