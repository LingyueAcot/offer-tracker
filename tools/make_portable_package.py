# -*- coding: utf-8 -*-
r"""打「便携包」：内置 Python 运行时 + 预装依赖 + 公开副本的 app\，用户解压双击就能用。

【为什么是脚本而不是手敲一遍】2C 是手工一步步试出来的（下载运行时 → 离线装依赖 → 拷 app →
写启动脚本 → 起实例验证）。手工步骤下次就得重来一遍，而且**漏一步不会报错**。
这个脚本把那一串固定下来，并在打包前后自查（漏数据文件、漏依赖都会在这里报错退出）。

【两条不能破】
  1. **包里绝不能有用户数据**：`data\`、`logs\`、`个人画像.json`、`config.json` 的**个人版**
     一个都不进；包里的 `config.json` 只放公开默认值（`build=release`）。
  2. **启动脚本必须隔离环境**：清 `PYTHONPATH`、设 `PYTHONNOUSERSITE=1`，
     否则便携运行时会吃到机器上的用户级 site-packages 与全局 PYTHONPATH（2C 实测）。

用法：
    python tools\make_portable_package.py                       # 默认拿 <workspace>\offer-tracker
    python tools\make_portable_package.py --source <公开副本> --out <输出目录>
    python tools\make_portable_package.py --no-zip              # 只铺目录，不打 zip（调试用）
"""
from __future__ import print_function

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SOURCE = r"<workspace>\offer-tracker"
DEFAULT_OUT = r"<private>\packages"
DEFAULT_CACHE = r"<private>\cache"
WHEELS = os.path.join(ROOT, "app", "wheels")

# 运行时来源（2026-10-11 定案）：python-build-standalone 的 3.12 精简版
RUNTIME_TAG = "20261009"
RUNTIME_NAME = "cpython-3.12.15+%s-x86_64-pc-windows-msvc-install_only_stripped.tar.gz" % RUNTIME_TAG
RUNTIME_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/%s/%s"
               % (RUNTIME_TAG, RUNTIME_NAME))
PACKAGE_NAME = "应届海投管理-便携版"
APPENDIX_MARK = "__APPENDIX__"      # 占位：该组件的条款全文另附在文件末尾

# 便携启动脚本：内容**只用 ASCII**（cmd.exe 在中文代码页下会把中文 rem 行解析断）
LAUNCHER = r"""@echo off
rem Portable launcher: use the bundled runtime, stay away from the machine's Python.
setlocal
set "HERE=%~dp0"
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
set "PYTHONIOENCODING=utf-8"
"%HERE%runtime\python\pythonw.exe" "%HERE%app\launcher.py"
"""

# 包里绝对不允许出现的东西（打包前后各查一遍）
FORBIDDEN = ["data", "logs", "webview_profile", "browser_profile", "个人画像.json", "__pycache__"]


def write_licenses(stage, runtime_dir):
    """往包里写 `licenses/`：Python 的 PSF 许可正文 + 第三方清单（名字/版本/许可/来源）。

    为什么必须做：我们自己那份代码是 MIT，但**包里分发着别人的东西**（CPython、pywebview、
    pythonnet、WebView2 的 DLL…）。元数据里没带许可正文的包，就如实写"以元数据与上游仓库为准"，
    **不替别人编**许可条款。
    """
    lic_dir = os.path.join(stage, "licenses")
    os.makedirs(lic_dir, exist_ok=True)
    # 1) CPython
    py_lic = os.path.join(runtime_dir, "python", "LICENSE.txt")
    rows = []
    appendix = []
    if os.path.isfile(py_lic):
        shutil.copyfile(py_lic, os.path.join(lic_dir, "Python-LICENSE.txt"))
        # ⚠️ 这一下也会 import 标准库 → 会重建 .pyc 并写进包内的构建路径。
        #    所以：① 带上 PYTHONDONTWRITEBYTECODE；② 瘦身步骤安排在**这一步之后**（双保险，2026-10-11 踩过）。
        lic_env = dict(os.environ)
        lic_env["PYTHONDONTWRITEBYTECODE"] = "1"
        rows.append(("CPython", subprocess.check_output(
            [os.path.join(runtime_dir, "python", "python.exe"), "-c",
             "import sys;print('%d.%d.%d' % sys.version_info[:3])"], env=lic_env).decode().strip(),
            "PSF-2.0", "https://www.python.org/", "licenses/Python-LICENSE.txt"))
    # 2) site-packages 里的每个包
    site = os.path.join(runtime_dir, "python", "Lib", "site-packages")
    import email as email_mod
    if os.path.isdir(site):
        for name in sorted(os.listdir(site)):
            if not name.endswith(".dist-info"):
                continue
            meta = os.path.join(site, name, "METADATA")
            if not os.path.isfile(meta):
                continue
            with io.open(meta, encoding="utf-8", errors="ignore") as handle:
                msg = email_mod.message_from_file(handle)
            pkg = msg.get("Name") or name
            ver = msg.get("Version") or ""
            raw_lic = (msg.get("License") or "")
            expr = (msg.get("License-Expression") or "").strip()
            classes = [c for c in (msg.get_all("Classifier") or []) if c.startswith("License ::")]
            flat = " ".join(raw_lic.split())
            # 标识符的选取顺序：PEP 639 的表达式 → 分类器 → 短的 License 字段 → 如实说"没声明"。
            # ⚠️ 有的包（如 pywebview）把**整篇条款**塞在 License 字段里，直接进表格会把表格撑成一坨，
            #    所以长文本另附到文件末尾（原样照抄，不改一个字）。
            if expr:
                ident = expr
            elif classes:
                ident = classes[0].split("::")[-1].strip()
            elif flat and len(flat) <= 60:
                ident = flat
            elif flat:
                ident = APPENDIX_MARK
            else:
                ident = "（元数据未声明，以上游仓库为准）"
            # 长正文**一律另附**（不管表格里显示的是不是短标识符）：
            # BSD 这类许可要求"二进制分发必须复制版权声明与条款"，pywebview 正好把全文放在 License 字段里。
            if flat and len(flat) > 200:
                appendix.append((pkg, ver, raw_lic))
                if ident not in (APPENDIX_MARK,):
                    ident = "%s（全文见本节末尾）" % ident
            url = (msg.get("Home-page") or "").strip()
            for u in (msg.get_all("Project-URL") or []):
                if "," in u:
                    url = url or u.split(",", 1)[1].strip()
            rows.append((pkg, ver, ident, url or "-", "上游仓库"))
    lines = [
        "# 第三方组件与许可（便携包里分发的东西）\n",
        "\n本项目的**自有代码**采用 MIT（见包根 `LICENSE`）。下面是随便携包一起分发的第三方组件。\n",
        "其中元数据里**没有**声明许可的，我们如实标注「以上游仓库为准」，**不替它写条款**。\n",
        "\n| 组件 | 版本 | 许可 | 来源 |\n| --- | --- | --- | --- |\n",
    ]
    for name, ver, ident, url, where in rows:
        lines.append("| %s | %s | %s | %s |\n" % (name, ver, ident, ("[%s](%s)" % (url, url)) if url.startswith("http") else where))
        if ident == APPENDIX_MARK:
            lines[-1] = "| %s | %s | %s | %s |\n" % (name, ver, "（条款全文见本节末尾）", ("[%s](%s)" % (url, url)) if url.startswith("http") else where)
    lines += [
        "\n## 需要单独说明的两件事\n",
        "\n1. **Microsoft Edge WebView2**：`pywebview` 的 Windows 后端自带 "
        "`Microsoft.Web.WebView2.Core.dll` / `Microsoft.Web.WebView2.WinForms.dll`（微软的 SDK，随其条款分发）。"
        "**WebView2 运行时本身不在本包里** —— 它是系统组件（Win11 自带、Win10 一般随 Edge 装好）。\n",
        "2. **界面渲染**与**导出 PDF** 都走系统上的 WebView2/浏览器，不走本包里的代码路径。\n",
        "\n> 想要逐字条款：`licenses/Python-LICENSE.txt` 是 CPython 的全文；其余组件看上表来源链接。\n",
    ]
    if appendix:
        lines.append("\n## 附：上游元数据里附带的许可正文（原样照抄）\n")
        for name, ver, text in appendix:
            lines.append("\n### %s %s\n\n```\n%s\n```\n" % (name, ver, text.strip()))
    with io.open(os.path.join(lic_dir, "THIRD-PARTY.md"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(lines))
    return len(rows)


# 一键升级入口：**必须放在被替换的目录之外**（否则替换时会把正在跑的自己换掉/锁住）。
# 内容只用 ASCII：cmd.exe 在中文代码页下会把中文按 GBK 解，**连路径里的中文都会解析错**。
# 所以更新器目录名也用 ASCII（`updater\`），只有这个 .cmd 的**文件名**是中文（用户看得懂，cmd 不解析它）。
UPGRADE_CMD = r"""@echo off
rem One-click in-place upgrade for the portable package.
rem Usage: put this package NEXT TO the old one, then double-click this file.
setlocal
set "HERE=%~dp0"
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
set "PYTHONIOENCODING=utf-8"
"%HERE%runtime\python\python.exe" "%HERE%updater\updater.py" --payload "%HERE%" --target "%~1"
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" echo [OK] Upgrade finished.
if "%CODE%"=="1" echo [X] Upgrade refused or rolled back - the old version still works.
if "%CODE%"=="2" echo [X] Rollback was incomplete - KEEP THE FOLDER, ask for help.
pause
"""


def write_updater(stage, source=None):
    """把更新器、更新清单、一键升级入口放进包里。

    为什么更新器要单独一个目录：方案 §3.5.1 第 3 条 —— **执行替换的那个程序必须在被替换的目录之外**。
    便携包里它住 `updater\`（用 `runtime\python\python.exe` 跑），只替换 `app\`。目录名用 ASCII：
    这样 `一键升级.cmd` 里就不出现中文，cmd.exe 按 GBK 解也不会解错路径。
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # ⚠️ **优先用"正在打包的那棵树"里的更新器**（公开副本那份已过脱敏改写），
    #    别直接从私有仓库拷：私有版有「这台电脑」这类私密词，会把它们带进发布包
    #    （2026-10-12 发布包门抓到 `updater/updater.py：私有词典：这台电脑`）。
    src_updater = ""
    for cand in (os.path.join(source or "", "tools", "updater.py"),
                 os.path.join(repo, "tools", "updater.py")):
        if cand and os.path.isfile(cand):
            src_updater = cand
            break
    if not src_updater:
        raise SystemExit("找不到更新器源码（看过 %s\\tools\\updater.py）" % (source or repo))
    upd_dir = os.path.join(stage, "updater")
    os.makedirs(upd_dir, exist_ok=True)
    shutil.copyfile(src_updater, os.path.join(upd_dir, "updater.py"))
    with io.open(os.path.join(stage, "一键升级.cmd"), "w", encoding="ascii", newline="\r\n") as handle:
        handle.write(UPGRADE_CMD)

    # 更新清单：**白名单**，只列 app\ 下的程序文件。
    # ⚠️ **用户文件一个都不许进清单**（`config.json`、`个人画像.json`、`data\`、`logs\`、profile…）：
    #    它们既不该被替换、也不该进回滚范围。判据与升级器**共用** `updater.is_user_file`（只此一处）。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import updater as _updater
    app_dir = os.path.join(stage, "app")
    files = {}
    skipped_user = []
    for dirpath, dirnames, filenames in os.walk(app_dir):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__",)]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, stage).replace("\\", "/")
            if _updater.is_user_file(rel):
                skipped_user.append(rel)
                continue
            files[rel] = sha256_file(full)
    if skipped_user:
        print("   （按用户文件规则排除 %d 项，不会进更新清单：%s%s）"
              % (len(skipped_user), "、".join(skipped_user[:4]), "…" if len(skipped_user) > 4 else ""))
    version = read_app_version(os.path.join(app_dir, "server.py"))
    with io.open(os.path.join(stage, "update-manifest.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"version": version, "from_version": "", "files": files, "removed": []},
                  handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return len(files)


def read_app_version(server_py):
    try:
        with io.open(server_py, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if line.startswith("APP_VERSION"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# 运行时要砍掉的东西（都在 runtime\python 下，相对该目录）
# 为什么砍：
#   · `Lib/__pycache__` —— 运行期会自己重建；而且**打包含路径会被写进 .pyc 的 co_filename**
#     （2026-10-11 实测：包里的 .pyc 带着打包机自己的目录路径），既是隐私也是脏。
#   · `Lib/test*`、`DLLs/_test*.pyd` —— 上游 CI 构建机的**用户目录路径**烙在里面（不是我这边的东西），我们不用测试模块。
#   · `ensurepip` / `idlelib` / `tkinter` / `tcl` —— 便携版用不到（界面是 pywebview/WebView2），白占体积。
TRIM = [
    "Lib/__pycache__", "Lib/test", "Lib/tests", "Lib/idlelib", "Lib/ensurepip",
    "Lib/tkinter", "tcl", "Tools", "Doc", "Scripts",
]
# `Scripts/` 为什么也砍：pip 生成的控制台脚本（`bottle.exe` 等）里把**解释器绝对路径**
# 写进了 shebang/嵌在 exe 里 —— 2026-10-11 实测那些 exe 带着我的构建路径。程序只用 pythonw + launcher.py 起，不需要它们。


def trim_runtime(runtime_dir):
    """砍掉运行时里用不到的部分。返回 (砍掉的字节数, 砍掉的条目数)。"""
    py_dir = os.path.join(runtime_dir, "python")
    removed = count = 0

    def drop(path):
        nonlocal removed, count
        if not os.path.exists(path):
            return
        if os.path.isdir(path):
            size = sum(os.path.getsize(os.path.join(d, f))
                       for d, _, fs in os.walk(path) for f in fs if os.path.isfile(os.path.join(d, f)))
            n = sum(len(fs) for _, _, fs in os.walk(path))
            shutil.rmtree(path, ignore_errors=True)
        else:
            size = os.path.getsize(path)
            n = 1
            os.remove(path)
        removed += size
        count += n

    for rel in TRIM:
        drop(os.path.join(py_dir, rel.replace("/", os.sep)))
    # 运行时里的其它 __pycache__（各子目录下）也清掉
    for dirpath, dirnames, _ in os.walk(runtime_dir):
        for d in list(dirnames):
            if d == "__pycache__":
                drop(os.path.join(dirpath, d))
                dirnames.remove(d)
    # 测试用的扩展模块
    dlls = os.path.join(py_dir, "DLLs")
    if os.path.isdir(dlls):
        for name in os.listdir(dlls):
            if name.startswith("_test") and name.endswith(".pyd"):
                drop(os.path.join(dlls, name))
    return removed, count


def count_tree(path):
    total = n = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                n += 1
            except OSError:
                pass
    return total, n
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def step(msg):
    print("\n== %s" % msg)


def download_runtime(cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    target = os.path.join(cache_dir, RUNTIME_NAME)
    if os.path.isfile(target) and os.path.getsize(target) > 10 * 1024 * 1024:
        print("   已缓存：%s（%.1f MB）" % (RUNTIME_NAME, os.path.getsize(target) / 1024.0 / 1024.0))
        return target
    print("   下载：%s" % RUNTIME_URL)
    code = subprocess.call(["curl.exe", "-L", "--fail", "--silent", "--show-error",
                            "-o", target, RUNTIME_URL])
    if code != 0 or not os.path.isfile(target):
        raise SystemExit("下载运行时失败（curl 退出码 %s）" % code)
    print("   下载完成：%.1f MB" % (os.path.getsize(target) / 1024.0 / 1024.0))
    return target


def check_source(source):
    """公开副本本身必须是干净的（没有数据/日志/画像）。"""
    bad = []
    for name in ("data", "logs", "webview_profile", "个人画像.json"):
        if os.path.exists(os.path.join(source, "app", name)):
            bad.append("app/" + name)
    if not os.path.isfile(os.path.join(source, "app", "launcher.py")):
        bad.append("（连 app/launcher.py 都没有 —— 源目录选错了？）")
    if bad:
        raise SystemExit("源目录不干净或不完整，拒绝打包：%s" % "、".join(bad))


def build(source, out_dir, cache_dir, wheels, make_zip=True, python_exe=None):
    check_source(source)
    stage = os.path.join(out_dir, PACKAGE_NAME)
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    os.makedirs(stage)

    step("① 取运行时")
    tarball = download_runtime(cache_dir)
    runtime_dir = os.path.join(stage, "runtime")
    os.makedirs(runtime_dir)
    code = subprocess.call(["tar.exe", "-xzf", tarball, "-C", runtime_dir])
    if code != 0:
        raise SystemExit("解压运行时失败（tar 退出码 %s）" % code)
    py = os.path.join(runtime_dir, "python", "python.exe")
    if not os.path.isfile(py):
        raise SystemExit("解压后找不到 runtime\\python\\python.exe")
    ver = subprocess.check_output([py, "-c", "import sys;print(sys.version.split()[0])"]).decode().strip()
    print("   运行时 Python %s" % ver)

    step("② 离线预装依赖（不让用户首启去 pip）")
    if not os.path.isdir(wheels):
        raise SystemExit("找不到 wheel 目录：%s" % wheels)
    # ⚠️ PYTHONDONTWRITEBYTECODE=1：否则 pip 这一步会就地生成 .pyc，
    #    而 .pyc 里会把**我的构建路径**写进 co_filename（2026-10-11 实测 2301 处）。
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = subprocess.call([py, "-m", "pip", "install", "--quiet", "--no-index",
                            "--find-links", wheels, "--no-warn-script-location",
                            "pywebview", "openpyxl"], env=env)
    if code != 0:
        raise SystemExit("离线安装依赖失败（pip 退出码 %s）" % code)
    check = subprocess.check_output([py, "-c",
        "import importlib.metadata as m;print(','.join(sorted(p+'='+m.version(p) for p in "
        "('pywebview','pythonnet','clr-loader','cffi','bottle','proxy-tools','openpyxl'))))"],
        env=env).decode().strip()
    print("   已装：%s" % check)

    step("③ 拷公开副本的 app\\（只有程序，没有任何数据）")
    shutil.copytree(os.path.join(source, "app"), os.path.join(stage, "app"))
    pyc = 0
    for dirpath, dirnames, filenames in os.walk(os.path.join(stage, "app")):
        for d in list(dirnames):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                dirnames.remove(d)
                pyc += 1
    print("   已拷入 app\\（清掉 %d 个 __pycache__）" % pyc)

    step("④ 写便携启动脚本 + 版本说明 + licenses\\ 许可清单")
    with io.open(os.path.join(stage, "启动（便携）.cmd"), "w", encoding="ascii", newline="\r\n") as handle:
        handle.write(LAUNCHER)
    print("   启动（便携）.cmd 已写（纯 ASCII 内容）")
    # ⚠️ 让包**明确声明**"数据放在我自己旁边"（config.json 的 data_dir）。
    #    2026-10-11 外部评审点出来的洞：不声明的话，一个干净的便携包既没有 data\、配置里也没有 data_dir，
    #    于是 `app/paths.py` 会判定成"新装"、把数据写进 `%LOCALAPPDATA%\ResumeTracker\` ——
    #    包看着是自带的，数据却留在了那台电脑上（换台机器/把包复制走就"丢数据"）。
    #    写相对路径 "data" 是**显式声明**，不靠猜。
    cfg_path = os.path.join(stage, "app", "config.json")
    with io.open(cfg_path, "r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    cfg["data_dir"] = "data"
    with io.open(cfg_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(cfg, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("   config.json 已声明 data_dir=data（便携包自带数据，不写用户目录）")
    n_lic = write_licenses(stage, runtime_dir)
    print("   licenses\\：CPython 许可正文 + 第三方清单（%d 个组件）" % n_lic)

    # 瘦身放在**最后**：前面任何一步只要 import 过标准库（例如取版本号），就会重建 .pyc
    # 并把**构建机的路径**写进去。放这里才能保证包里的字节码是干净的（2026-10-11 踩过两次）。
    step("④·5 瘦身：砍掉运行时里用不到的东西")
    removed, items = trim_runtime(runtime_dir)
    after_mb = count_tree(runtime_dir)[0] / 1024.0 / 1024.0
    print("   砍掉 %.1f MB / %d 个条目 → runtime 现在 %.1f MB" % (removed / 1024.0 / 1024.0, items, after_mb))
    if removed == 0:
        print("   [WARN] 一个都没砍到 —— 是不是运行时的目录结构变了？去看一眼 TRIM 清单")
    print("   （砍掉的是 __pycache__/test/ensurepip/idlelib/tkinter/tcl/Scripts 与 _test*.pyd，便携版用不到）")

    step("④·6 更新器随包 + 更新清单 + 一键升级入口")
    n_upd = write_updater(stage, source)
    print("   更新器：updater\\updater.py + 一键升级.cmd + update-manifest.json（%d 个 app 文件进清单）" % n_upd)

    step("⑤ 打包前自查：包里不能有数据/日志/画像")
    found = []
    for dirpath, dirnames, filenames in os.walk(stage):
        rel = os.path.relpath(dirpath, stage).replace("\\", "/")
        for name in list(dirnames) + filenames:
            if name in FORBIDDEN and not rel.startswith("runtime"):
                found.append(os.path.join(rel, name).lstrip("./"))
    if found:
        raise SystemExit("包里出现了不该有的东西：%s" % "、".join(found))
    print("   干净 ✅（只查了 app\\ 与包根；runtime 里的 __pycache__ 是运行时的，不算）")

    step("⑥ 生成清单与校验和")
    manifest = {}
    for dirpath, dirnames, filenames in os.walk(stage):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, stage).replace("\\", "/")
            manifest[rel] = sha256_file(full)
    lines = ["%s  %s" % (digest, rel) for rel, digest in sorted(manifest.items())]
    with io.open(os.path.join(stage, "SHA-256SUMS.txt"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    with io.open(os.path.join(stage, "manifest.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"package": PACKAGE_NAME, "runtime": RUNTIME_NAME, "files": manifest},
                  handle, ensure_ascii=False, indent=2)
    total = sum(os.path.getsize(os.path.join(stage, r)) for r in manifest)
    print("   文件 %d 个 / %.1f MB；SHA-256SUMS.txt 与 manifest.json 已写" % (len(manifest), total / 1024.0 / 1024.0))

    zip_path = ""
    if make_zip:
        step("⑦ 打 zip")
        zip_path = os.path.join(out_dir, PACKAGE_NAME + ".zip")
        if os.path.isfile(zip_path):
            os.remove(zip_path)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for rel in sorted(manifest):
                z.write(os.path.join(stage, rel), os.path.join(PACKAGE_NAME, rel))
            for extra in ("SHA-256SUMS.txt", "manifest.json"):
                z.write(os.path.join(stage, extra), os.path.join(PACKAGE_NAME, extra))
        zip_hash = sha256_file(zip_path)
        print("   %s（%.1f MB）" % (zip_path, os.path.getsize(zip_path) / 1024.0 / 1024.0))
        print("   SHA-256：%s" % zip_hash)
        with io.open(zip_path + ".sha256", "w", encoding="utf-8", newline="\n") as handle:
            handle.write("%s  %s\n" % (zip_hash, os.path.basename(zip_path)))

    print("\n完成。目录：%s" % stage)
    if zip_path:
        print("      压缩包：%s" % zip_path)
    print("下一步（必做，别省）：解压到 C 盘本地目录起一次，确认能开、能关、能导出。")
    print("  ⚠️ 别在 E:\\AI work 这类被沙箱打标的路径下测 —— .NET 会把那里的程序集当'网络位置'拒绝加载（2C 实测）。")
    return 0


def main():
    parser = argparse.ArgumentParser(description="打便携包（内置 Python 运行时）")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="公开副本目录")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    parser.add_argument("--wheels", default=WHEELS)
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args()
    if not os.path.isdir(args.source):
        raise SystemExit("源目录不存在：%s（先用 public_freeze.py 冻结出公开副本）" % args.source)
    return build(args.source, args.out, args.cache, args.wheels, not args.no_zip)


if __name__ == "__main__":
    sys.exit(main())
