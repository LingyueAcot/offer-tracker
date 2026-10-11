# -*- coding: utf-8 -*-
r"""发布包「首启自检」：把包解到一个干净目录，真启动一次，逐条核对。

【为什么要有它】便携包能不能用，**看代码看不出来**：依赖漏装、运行时缺文件、启动脚本写错，
都要真的起一次才知道。2026-10-11 那次是我手工一条条敲命令验的 —— 手工步骤下次还得重来，
而且很容易漏（比如"包里居然有 data/"这种事）。这个脚本把它固定下来。

【它查什么】
  起之前：包里不许有 用户数据/日志/画像/数据库；运行时与启动脚本必须在。
  起之后：`/api/state` 通、`edition=release`、首启**自建空库**（0 条投递）、窗口**真渲染出来了**（看启动日志）。

【两条环境要求（脚本会自己先查）】
  1. **必须在本地盘跑**：包若放在被安全软件/沙箱打标的路径（例如这台电脑 `<workspace>`），
     .NET 会把程序集当"网络位置"拒绝加载，程序起不来（2C 实测）。所以本脚本**总是把包复制到
     `%TEMP%` 再启动**，并且不让被测包所在盘影响结论。
  2. **8768 必须空着**：包里 `config.json` 用的是 8768；被占就明确报出来，不硬闯（用户可能正开着自己的程序）。

用法：
    python tools\selfcheck_package.py <包.zip 或 解开的目录> [--port 8768] [--keep]
"""
from __future__ import print_function

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ok = True
skipped = 0


def check(label, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("[PASS] " if cond else "[FAIL] ") + label + ("  " + extra if extra else ""))
    return cond


def skip(label, extra=""):
    global skipped
    skipped += 1
    print("[SKIP] " + label + ("  " + extra if extra else ""))


def get_json(port, path, timeout=5):
    url = "http://127.0.0.1:%d%s" % (port, path)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def wait_ready(port, seconds=60):
    """显式等待：每轮都说话（不许静默轮询 —— AGENTS.md 三·五节）。"""
    deadline = time.time() + seconds
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            status, state = get_json(port, "/api/state", timeout=4)
            print("  第 %d 次探测：HTTP %s（已就绪）" % (attempt, status))
            return state
        except Exception as err:                     # noqa: BLE001 - 就绪前什么都可能
            print("  第 %d 次探测：还没起来（%s）" % (attempt, type(err).__name__))
            time.sleep(3)
    return None


def find_exe():
    for cand in ("pythonw.exe", "python.exe"):
        found = glob.glob(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", cand))
        if found:
            return cand
    return "pythonw.exe"


def kill_by_dir(path):
    """按命令行杀进程。⚠️ 不能按可执行文件路径筛 —— 便携版的 `pythonw.exe` 在包内，
    但重开后的父进程可能来自别处；按命令行匹配最可靠（2026-10-11 踩过）。"""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*%s*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
          % path.replace("'", "''"))
    subprocess.call(["powershell", "-NoProfile", "-Command", ps])


def wait_log(path, needle, seconds=20):
    """等启动日志里出现某句话。

    ⚠️ 别在 `/api/state` 一通之后就立刻读日志：接口先就绪、**首屏渲染晚一两秒**才写进日志。
    早先就是抢跑，同一份包一次过、一次报 FAIL（2026-10-11 实测）。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        text = ""
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                text = handle.read().decode("utf-8", "ignore")
        if needle in text:
            return text
        time.sleep(1)
    return text if os.path.isfile(path) else ""


def main():
    parser = argparse.ArgumentParser(description="发布包首启自检")
    parser.add_argument("package", help="包 .zip 或已解开的目录")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--keep", action="store_true", help="跑完保留解出来的目录（排查用）")
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()

    src = os.path.abspath(args.package)
    if not os.path.exists(src):
        raise SystemExit("找不到：%s" % src)

    print("== 0) 先把包解到 %%TEMP%%（本地盘）＝=")
    work = tempfile.mkdtemp(prefix="rz-selfcheck-")
    payload = os.path.join(work, "pkg")
    if os.path.isdir(src):
        shutil.copytree(src, payload)
    elif src.lower().endswith(".zip"):
        with zipfile.ZipFile(src) as z:
            z.extractall(payload)
    else:
        raise SystemExit("只认目录或 .zip：%s" % src)
    tops = [d for d in os.listdir(payload) if os.path.isdir(os.path.join(payload, d))]
    root = os.path.join(payload, tops[0]) if len(tops) == 1 else payload
    print("   包根：%s" % root)

    print("\n== 1) 起之前的静态检查 ＝=")
    app = os.path.join(root, "app")
    runtime_py = os.path.join(root, "runtime", "python", "python.exe")
    check("运行时在（runtime\\python\\python.exe）", os.path.isfile(runtime_py))
    launcher_cmd = [n for n in os.listdir(root) if n.lower().endswith(".cmd")]
    # ⚠️ 2G 往包里加了 `一键升级.cmd` 之后必须**排除它**：
    #    之前这里取 `launcher_cmd[0]`（os.listdir 的顺序），会挑中升级器去"启动"，
    #    结果永远是"实例起不来、等 90 秒超时" —— 2026-10-12 应用户要求做发布包首启验收时踩到。
    run_cmd = [n for n in launcher_cmd if "升级" not in n]
    check("有便携启动脚本", bool(launcher_cmd), "、".join(launcher_cmd))
    check("启动脚本能认出来（排除「一键升级.cmd」）", bool(run_cmd), "候选：%s" % "、".join(run_cmd))
    for bad, why in (("data", "用户数据"), ("logs", "日志"), ("webview_profile", "浏览器 profile"),
                     ("browser_profile", "浏览器 profile"), ("个人画像.json", "个人画像")):
        check("包里没有 %s（%s）" % (bad, why), not os.path.exists(os.path.join(app, bad)))
    dbs = [p for p in glob.glob(os.path.join(root, "**", "*.db"), recursive=True)
           if os.sep + "runtime" + os.sep not in p]
    check("包里没有数据库文件", not dbs, "、".join(dbs[:3]))
    check("包里有 licenses\\ 许可清单", os.path.isdir(os.path.join(root, "licenses")))
    # 便携包必须**明确声明**"数据在自己旁边"，否则程序会按"新装"把数据写进用户目录（外部评审 #1）
    try:
        with open(os.path.join(app, "config.json"), "rb") as handle:
            cfg = json.loads(handle.read().decode("utf-8"))
    except Exception as err:                                    # noqa: BLE001
        cfg = {}
        check("能读到 app\\config.json", False, str(err))
    check("config.json 声明了 data_dir=data（便携＝数据自带）",
          str(cfg.get("data_dir", "")).replace("\\", "/").strip("/") == "data",
          "实际 data_dir=%r" % cfg.get("data_dir"))
    # 更新器必须**放在被替换的目录之外**（方案 §3.5.1 第 3 条）：便携包里住 `updater\`，只替换 app\
    check("更新器在（updater\\updater.py，且在 app\\ 之外）",
          os.path.isfile(os.path.join(root, "updater", "updater.py")))
    check("更新器不在 app\\ 里（否则替换时会把自己换掉）",
          not os.path.exists(os.path.join(app, "updater.py")))
    check("有一键升级入口",
          any(n.endswith(".cmd") and "升级" in n for n in os.listdir(root)))
    man_path = os.path.join(root, "update-manifest.json")
    man_ok = False
    if os.path.isfile(man_path):
        try:
            with open(man_path, "rb") as handle:
                man = json.loads(handle.read().decode("utf-8"))
            files = man.get("files") or {}
            bad = [k for k in files if not k.startswith("app/")]
            man_ok = bool(files) and not bad
            if bad:
                print("      （清单里有非 app\\ 路径：%s）" % "、".join(bad[:4]))
        except Exception as err:                                # noqa: BLE001
            print("      （读不了 update-manifest.json：%s）" % err)
    check("有更新清单（白名单只含 app\\ 下的文件）", man_ok)

    print("\n== 2) 端口 %d 必须空着（可能你正开着自己的程序）＝=" % args.port)
    try:
        get_json(args.port, "/api/state", timeout=3)
        print("[FAIL] %d 上已经有程序在跑 —— 先关掉再自检（不硬闯）" % args.port)
        shutil.rmtree(work, ignore_errors=True)
        return 1
    except Exception:                                # noqa: BLE001
        check("%d 空着" % args.port, True)

    print("\n== 3) 真启动（双击那个 .cmd）＝=")
    cmd = os.path.join(root, run_cmd[0]) if run_cmd else os.path.join(root, "启动（便携）.cmd")
    subprocess.Popen(["cmd.exe", "/c", cmd], cwd=root,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    state = wait_ready(args.port, args.timeout)
    if not check("实例起来了（/api/state 通）", state is not None,
                 "" if state else "等了 %d 秒；去看 app\\logs\\launcher.log" % args.timeout):
        kill_by_dir(root)
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        print("\n结果：存在失败项")
        return 1

    print("\n== 4) 起来之后核对 ＝=")
    check("edition 是 release（不是开发版）", state.get("edition") == "release", str(state.get("edition")))
    check("desktop 标记为真（真的在桌面外壳里跑）", state.get("desktop") is True)
    total = int(state.get("stats", {}).get("total", -1))
    check("首启是空库（0 条投递，说明没夹带别人的数据）", total == 0, "投递=%s" % total)
    db = os.path.join(app, "data", "applications.db")
    check("程序自建了数据库", os.path.isfile(db),
          "%d 字节" % os.path.getsize(db) if os.path.isfile(db) else "没有")
    log = os.path.join(app, "logs", "launcher.log")
    text = wait_log(log, "页面已渲染", 20)
    check("窗口真渲染出来了（启动日志里有「页面已渲染」）", "页面已渲染" in text)
    check("外壳桥通了（JS→Python）", "外壳桥" in text)

    print("\n== 5) 收尾：关掉它 ＝=")
    kill_by_dir(root)
    time.sleep(2)
    try:
        get_json(args.port, "/api/state", timeout=3)
        check("已关闭（端口释放）", False, "还通着")
    except Exception:                                # noqa: BLE001
        check("已关闭（端口释放）", True)
    if args.keep:
        print("   保留目录（--keep）：%s" % work)
    else:
        shutil.rmtree(work, ignore_errors=True)

    print("\n结果：" + ("全部通过" if ok else "存在失败项") + ("（跳过 %d 项）" % skipped if skipped else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
