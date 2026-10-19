# -*- coding: utf-8 -*-
"""
pywebview 接口探测 + 真实开窗测试（阶段 2 开工前的一次性检查）。

【为什么要这个】
写 v3 的启动器要用到 pywebview 的窗口参数名、事件名、settings 键名，这些在不同
版本之间会变。凭记忆写容易写出「双击没反应」的代码。所以先让这台电脑 Python 3.12
自己把真实接口报出来，写进同目录的 pywebview_api.txt，写启动器时照着写。

【怎么用】
直接双击 work\\probe_pywebview.cmd 就行（探接口 + 开窗一次做完）。
不想弹窗口、只探接口的话，在命令行里加 --api-only。

【它会做什么】
  1. 看 v3 的服务（默认 8768）在不在，不在就用 pythonw 起一个；
  2. 用 pywebview 打开 http://127.0.0.1:8768/，1280x860；
  3. 窗口真的建起来之后，记下窗口句柄、位置、DPI；
  4. 你手动关掉那个窗口，脚本记下「从开到关用了多久」然后退出。
  这一步在验证「pywebview 能不能当这个程序的外壳」，不影响 2.0。
"""
import inspect
import json
import os
import subprocess
import sys
import threading
import time
import traceback

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "work", "pywebview_api.txt")
V3_DIR = os.path.join(BASE, "app")

LINES = []


def add(text=""):
    LINES.append(str(text))


def add_section(title):
    add()
    add("=" * 68)
    add(title)
    add("=" * 68)


def flush():
    try:
        with open(OUT, "w", encoding="utf-8") as handle:
            handle.write("\n".join(LINES))
    except Exception:
        add("写文件失败：%s" % traceback.format_exc())
    try:
        print("\n".join(LINES))
    except Exception:
        pass


def probe_api():
    add_section("环境")
    add("Python    : %s" % sys.version.replace("\n", " "))
    add("解释器    : %s" % sys.executable)
    add("项目根目录: %s" % BASE)

    add_section("导入 pywebview")
    try:
        import webview
    except Exception:
        add("导入失败：")
        add(traceback.format_exc())
        add("先装： pip install pywebview")
        return None

    add("版本      : %s" % getattr(webview, "__version__", "?"))
    add("模块路径  : %s" % getattr(webview, "__file__", "?"))

    add_section("顶层函数签名（写启动器照这个写）")
    for name in ("create_window", "start", "set_title", "active_window"):
        fn = getattr(webview, name, None)
        if fn is None:
            add("%-16s <没有这个函数>" % name)
            continue
        try:
            add("%-16s %s" % (name, inspect.signature(fn)))
        except Exception as exc:
            add("%-16s <读签名失败 %r>" % (name, exc))

    add_section("webview.settings（默认值）")
    try:
        settings = webview.settings
        add("类型: %s" % type(settings).__name__)
        for key in sorted(settings):
            add("  %-34s = %r" % (key, settings[key]))
    except Exception:
        add("读 settings 失败：%s" % traceback.format_exc())

    add_section("Window 对象有哪些方法")
    try:
        from webview.window import Window
        add("、".join(sorted(n for n in dir(Window) if not n.startswith("_"))))
        add()
        for name in ("load_url", "load_html", "evaluate_js", "destroy", "hide",
                     "show", "minimize", "restore", "resize", "move",
                     "toggle_fullscreen", "set_title", "get_position"):
            add("  %-20s %s" % (name, "有" if hasattr(Window, name) else "没有"))
    except Exception:
        add("读 Window 失败：%s" % traceback.format_exc())

    add_section("Window 的事件")
    try:
        from webview.window import Window
        events = getattr(Window, "events", None)
        add("events 类型: %s" % type(events).__name__)
        if events is not None:
            add("可用事件: %s" % "、".join(
                sorted(n for n in dir(events) if not n.startswith("_"))))
    except Exception:
        add("读事件失败：%s" % traceback.format_exc())

    add_section("后端")
    try:
        import webview.platforms as platforms
        add("platforms 路径: %s" % getattr(platforms, "__path__", "?"))
    except Exception as exc:
        add("platforms 读取失败：%r" % exc)
    try:
        add("webview.guilib: %r" % getattr(webview, "guilib", "<无>"))
    except Exception as exc:
        add("guilib 读取失败：%r" % exc)

    return webview


def read_v3_port():
    try:
        with open(os.path.join(V3_DIR, "config.json"), "r", encoding="utf-8") as handle:
            return int(json.load(handle).get("port") or 8768)
    except Exception:
        return 8768


def ping(port, timeout=1.5):
    try:
        from urllib.request import urlopen
        with urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=timeout) as res:
            return res.status == 200
    except Exception:
        return False


def window_test(webview):
    add_section("真实开窗测试")
    port = read_v3_port()
    add("v3 端口   : %d" % port)

    started_server = False
    if not ping(port):
        add("服务没在跑，用 pythonw 起一个...")
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        try:
            subprocess.Popen([pythonw, "server.py"], cwd=V3_DIR, close_fds=True)
            started_server = True
        except Exception:
            add("起服务失败：%s" % traceback.format_exc())
            return
        deadline = time.time() + 30
        while time.time() < deadline and not ping(port):
            time.sleep(0.5)
    if not ping(port):
        add("等不到服务起来，跳过开窗测试")
        return
    add("服务就绪  : http://127.0.0.1:%d/" % port)
    add("这个服务是脚本起的：%s" % ("是" if started_server else "否（本来就在跑）"))

    state = {"info": {}, "close_seconds": None}

    def watch():
        time.sleep(2.5)
        try:
            win = webview.windows[0]
            native = win.native
            state["info"]["native"] = repr(native)[:120]
            try:
                import ctypes
                import ctypes.wintypes as wt
                hwnd = int(native.Handle.ToInt64())
                state["info"]["hwnd"] = hwnd
                rect = wt.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                state["info"]["rect"] = [rect.left, rect.top, rect.right, rect.bottom]
                state["info"]["dpi"] = ctypes.windll.user32.GetDpiForWindow(hwnd)
            except Exception as exc:
                state["info"]["hwnd_error"] = repr(exc)
            add(">>> 窗口已经弹出来了，请手动关掉它（点右上角 ×）。")
        except Exception:
            state["info"]["window_error"] = traceback.format_exc()
            add("取窗口失败：%s" % traceback.format_exc())

    threading.Thread(target=watch, daemon=True).start()
    url = "http://127.0.0.1:%d/" % port
    t0 = time.time()
    try:
        webview.create_window("应届海投管理 v3（外壳测试）", url,
                              width=1280, height=860, min_size=(900, 640))
        webview.start()
    except Exception:
        add("开窗失败：%s" % traceback.format_exc())
        return
    state["close_seconds"] = round(time.time() - t0, 1)

    add()
    add("窗口已关闭，webview.start() 正常返回。")
    add("从开到关用了: %s 秒" % state["close_seconds"])
    for key in ("native", "hwnd", "rect", "dpi", "hwnd_error", "window_error"):
        if key in state["info"]:
            add("%-10s: %s" % (key, state["info"][key]))
    add("**没有报错 = pywebview 可以当这个程序的外壳。**")

    # 收尾：只关掉「这个脚本自己起的」服务。本来就在跑的（比如你自己双击开的）不动
    if started_server:
        try:
            from urllib.request import urlopen
            urlopen("http://127.0.0.1:%d/api/shutdown" % port, b"{}", timeout=5).read()
            add("已把脚本自己起的服务关掉，不留后台进程。")
        except Exception as exc:
            add("关服务失败（不影响结论）：%r" % exc)
    else:
        add("服务本来就是开着的，脚本不动它。")


def main():
    add("pywebview 接口探测")
    add("时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    do_window = "--api-only" not in sys.argv[1:]
    add("模式: %s" % ("接口 + 真实开窗" if do_window else "仅探接口（--api-only）"))
    webview = probe_api()
    if webview is not None and do_window:
        try:
            window_test(webview)
        except Exception:
            add("开窗测试异常：")
            add(traceback.format_exc())
    flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
