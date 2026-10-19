# -*- coding: utf-8 -*-
"""
services/diagnostics.py —— 排查现场：环境快照 / 错误日志 / 诊断报告（2026-09-25 新增）

【为什么有这一层】用户要一个「把别人电脑上的现场拿回来」的途径。
排查需要三样东西，都在这里采：
  1. **运行环境**（系统 / Python / 屏幕缩放 / WebView2 / 数据量）—— 见 `collect_env()`
  2. **错误日志** —— 见 `log_error()`（服务端异常和后台线程崩溃都往这儿写）
  3. **把上面打成一份文件** —— 见 `build_diagnostic_report()`

【规矩】
- 路径由 `server.py` 用 `use()` 注入（和 `store` / `audit` 同一套做法），
  这一层**不自己算路径**。
- 排版/截断/禁止内容的规则在 `core/diagnostics.py`（纯函数、能离线测），这里只做 I/O。
- **报告绝不包含**：`data/applications.db`、`data/change_log.jsonl`、`个人画像.json` 的内容。
  这一条由 `tools/test_diagnostics.py` 断言（拿真库抽样本当探针反过来查报告）。

【屏幕缩放（DPI）为什么值得记】2026-09-24 那个「最大化后底部一条黑横条」的 bug
**只在缩放不是 100% 的机器上出现**，开发者这台电脑 96 DPI 永远复现不了。
当时日志里要是有这一行，一眼就能锁定。
"""

import io
import json
import os
import platform
import sys
import threading
import time
import traceback

from core.diagnostics import build_report, format_env, tail

# 由 server.py 注入
_paths = {
    "logs": "",       # app/logs
    "data": "",       # app/data
    "error_log": "",  # app/data/error.log
    "server_log": "",  # app/logs/server.log
    "launcher_log": "",  # app/logs/launcher.log
}
_lock = threading.Lock()


def use(logs_dir, data_dir):
    """server.py 启动时调一次。"""
    _paths["logs"] = logs_dir
    _paths["data"] = data_dir
    _paths["error_log"] = os.path.join(data_dir, "error.log")
    _paths["server_log"] = os.path.join(logs_dir, "server.log")
    _paths["launcher_log"] = os.path.join(logs_dir, "launcher.log")


def error_log_path():
    return _paths["error_log"]


# --------------------------------------------------------------------------- #
# 环境采集
# --------------------------------------------------------------------------- #

def _screen_info():
    """
    屏幕尺寸 + 缩放 + 显示器数 + **进程的 DPI 感知级别**。

    ⚠️ 2026-09-27 改：原来直接 `GetDpiForSystem()`，而这个进程**默认不是 DPI 感知的** ——
    那种情况下 Windows 返回 96（=100%）和**虚拟化**的尺寸，于是缩放 150% 的机器上这份报告
    永远写着「缩放 96 DPI (100%)」，等于根本问不出"是不是缩放惹的祸"
    （2026-09-24 那个"最大化后底部黑条"的 bug 只在缩放 != 100% 的机器上出现，就是卡在这儿）。
    现在统一走 `winenv`（DPI 感知 + 显示器几何的唯一来源，见 winenv.py 顶部），
    并且 server 启动时会先声明感知（server.py 的 main()）。

    ⚠️ 非 Windows 或调用失败都返回"未知"，绝不抛异常 —— 采集环境信息这件事
    不能成为新的崩溃点（它本来就是为了排查崩溃才存在的）。
    """
    try:
        import winenv
        width, height = winenv.screen_size()
        text = "%d x %d" % (width, height)
        dpi = winenv.system_dpi()
        if dpi:
            # 96 DPI = 100%；这一项是排查「缩放相关」的窗口/像素问题的关键
            text += " · 缩放 %d DPI (%d%%)" % (dpi, round(dpi * 100.0 / 96))
        text += " · 进程 DPI 感知 %s" % winenv.awareness_name()
        work, scale = winenv.monitor_work_area()
        text += " · 主屏可用区域 %d,%d %dx%d（该屏缩放 %.0f%%）" % (
            work[0], work[1], work[2] - work[0], work[3] - work[1], (scale or 1.0) * 100)
        return text
    except Exception:
        return ""


def _display_count():
    try:
        import winenv
        return str(winenv.display_count())
    except Exception:
        return ""


def _virtual_screen():
    """
    「所有显示器拼起来的虚拟桌面」范围（**物理像素**，前提是进程已声明 DPI 感知）。

    ⚠️ 这是排查**多屏/双屏**问题的关键：屏幕显示，虚拟桌面就宽一倍
    （双 1920 屏 = 3840 宽），而且**副屏在主屏左边时 left 是负数**。
    窗口位置恢复时用到的正是这套坐标（见 launcher.py 的 position_is_visible），
    所以这一行能把"窗口跑到屏幕外/贴边角"这类问题一眼看出来。
    """
    try:
        import winenv
        left, top, width, height = winenv.virtual_screen()
        primary_w, primary_h = winenv.screen_size()
        return "虚拟桌面 %d,%d 起 · %d x %d（主屏 %d x %d）" % (
            left, top, width, height, primary_w, primary_h)
    except Exception:
        return ""


def _window_state():
    """读 `data/window_state.json`（记着上次的窗口位置/大小）。读不到就写"未知"。"""
    path = os.path.join(_paths["data"], "window_state.json")
    try:
        if not os.path.isfile(path):
            return "还没有（第一次启动或没退出过）"
        with io.open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return "内容不是对象"
        return "位置 %s,%s · 大小 %sx%s · 最大化=%s" % (
            data.get("x"), data.get("y"), data.get("w"), data.get("h"),
            data.get("maximized"))
    except Exception:
        return "读不到"


# 启动日志里这几行是「外壳控制通道好不好」的直接证据。
# 为什么要在报告里单独摘出来：窗口控件的拖动/最小化/最大化**全靠这条通道**，
# 而这条通道不通时的表现是"点了没反应"（页面 fetch 发完不管、连报错都没有），
# 所以必须主动把这几个信号摆到报告前面，否则得在几百行日志里翻。
_SHELL_HINTS = (
    "窗口控制服务已就绪",
    "页面已连上外壳桥",
    "无边框模式就绪",
    "已接管窗口消息",
    "拖动开始",
    "第一次按位移搬窗口成功",
    "拿不到窗口句柄",
    "收到不认识的窗口命令",
    "外壳控制通道",
    "自动改用",
)


def _shell_evidence():
    """从启动器日志里摘出与「窗口控件能不能用」有关的行（最多各一条）。"""
    text = _read_text(_paths["launcher_log"])
    if not text:
        return ""
    found = []
    for hint in _SHELL_HINTS:
        for line in text.splitlines():
            if hint in line:
                found.append("  " + line.strip()[:150])
                break
    if not found:
        return "（日志里没有任何窗口控件的记录 —— 说明启动链路可能没走到那一步）"
    return "\n".join(found)


def _webview2_version():
    """
    从注册表读 WebView2 运行时版本（窗口外壳依赖它）。

    读不到就返回空 —— 可能确实没装，也可能是非 Windows。两种情况都如实写「未知」。
    """
    try:
        import winreg
    except Exception:
        return ""
    sub = (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
           r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
    sub2 = (r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
            r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, sub),
                       (winreg.HKEY_CURRENT_USER, sub2)):
        try:
            key = winreg.OpenKey(hive, path)
            value, _ = winreg.QueryValueEx(key, "pv")
            if value:
                return str(value)
        except Exception:
            continue
    return ""


def collect_env(app_version="", edition="", desktop=None, counts=None, backup=None):
    """
    采一份运行环境。

    `counts` / `backup` 由 `server.py` 传进来（它才知道数据量和这次备份的结果）——
    这一层不去查库，避免又多一条连接。
    """
    desktop_text = "桌面模式" if desktop else ("浏览器模式" if desktop is not None else "")
    py = "%s (%d 位)" % (platform.python_version(),
                         64 if sys.maxsize > 2 ** 32 else 32)
    data_text = ""
    if counts:
        # ⚠️ 必须是 counts.items()：直接 `for k, v in counts` 在 Python 3 上
        # 只遍历 key，会 ValueError（自测时踩到）。
        data_text = " · ".join("%s %s" % (k, v) for k, v in counts.items())
    return {
        "app": "%s %s%s" % (app_version or "?",
                            "个人版" if edition == "personal"
                            else ("正式版" if edition == "release" else ""),
                            (" · " + desktop_text) if desktop_text else ""),
        "os": platform.platform(),
        "python": py,
        "screen": _screen_info(),
        "displays": _display_count() and (_display_count() + " 个") or "",
        "virtual": _virtual_screen(),
        "window": _window_state(),
        "webview2": _webview2_version(),
        "data": data_text,
        "backup": backup or "",
    }


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #

def _append(path, text):
    """追加一行/一段。写不进去也不能影响主流程（日志失败不该让程序崩）。"""
    if not path:
        return False
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with _lock:
            with io.open(path, "a", encoding="utf-8", errors="replace") as handle:
                handle.write(text)
        return True
    except Exception:
        return False


def log_error(where, exc_text, extra=""):
    """
    记一条错误到 `data/error.log`。

    为什么单独一个文件：排查时**不该在几千行日志里翻** ——
    `launcher.log` 现在 2500+ 行，里面 99% 是正常的窗口/端口记录。
    这个文件只在真出错时才长，打开就是问题本身。

    也顺手写进 stderr（也就是 `logs/server.log`），这样两处都有。
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    block = "\n---- %s  %s ----\n%s\n" % (stamp, where, (exc_text or "").rstrip())
    if extra:
        block = block.rstrip() + "\n" + extra.rstrip() + "\n"
    _append(_paths["error_log"], block)
    try:
        sys.stderr.write(block)
        sys.stderr.flush()
    except Exception:
        pass
    return block


def log_exception(where, exc=None):
    """把当前异常（或传进来的那个）连 traceback 一起记下。"""
    if exc is None:
        text = traceback.format_exc()
    else:
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return log_error(where, text)


def install_thread_hook():
    """
    让**后台线程**里没被捕获的异常也留下记录。

    为什么需要：Python 默认对线程里的异常只打印到 stderr，而桌面模式下
    stderr 原来是被丢掉的（现在接到 `logs/server.log` 了，但仍然没人主动看）。
    最主要的是：同步宣讲会、看门狗这些都是后台线程，它们静默崩掉的表现是
    「功能突然不动了」，最难查。
    """
    def hook(args):
        try:
            name = getattr(args.thread, "name", "?")
            log_error("后台线程 %s 崩溃" % name,
                      "".join(traceback.format_exception(
                          args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:
            pass
    try:
        threading.excepthook = hook
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def _file_info(path, label):
    """一行「文件多大了 / 多少行 / 什么时候改的」。读不到就写明读不到。"""
    try:
        if not os.path.isfile(path):
            return "  %-16s（还没有这个文件）" % label
        size = os.path.getsize(path)
        lines = 0
        with io.open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                lines += chunk.count(b"\n")
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
        return "  %-16s %8.1f KB · %6d 行 · 最后修改 %s" % (
            label, size / 1024.0, lines, mtime)
    except Exception:
        return "  %-16s（读不到）" % label


def _read_text(path, limit=None):
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        return text if limit is None else text[-limit:]
    except Exception:
        return ""


def build_diagnostic_report(env, app_version="", edition="", now=None):
    """
    打包一份诊断报告（纯文本）。返回值就是文件内容，调用方负责落盘/下载。

    ⚠️ **绝不读** `applications.db` / `change_log.jsonl` / `个人画像.json` ——
    这三样在这个函数里一次都不出现。要验证这一点看 `tools/test_diagnostics.py`。
    """
    files = "\n".join([
        _file_info(_paths["error_log"], "error.log"),
        _file_info(_paths["server_log"], "server.log"),
        _file_info(_paths["launcher_log"], "launcher.log"),
    ])
    return build_report(
        env=env,
        files=files,
        error_log=_read_text(_paths["error_log"]),
        server_log=_read_text(_paths["server_log"]),
        launcher_log=_read_text(_paths["launcher_log"]),
        generated_at=now or time.strftime("%Y-%m-%d %H:%M:%S"),
        app_version=app_version,
        edition=edition,
        shell_evidence=_shell_evidence(),
    )
