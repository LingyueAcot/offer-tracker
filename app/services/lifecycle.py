# -*- coding: utf-8 -*-
"""
services/lifecycle.py —— 桌面模式的生命周期状态（心跳 / 关闭信号 / 看门狗 / 退出钩子）

2026-09-24 从 `server.py` 搬过来（S4）。`api/desktop.py` 要用
`touch_heartbeat` / `touch_closing` / `request_shutdown`，原来只能
`from server import ...`；搬到这里，它就能直接依赖 `services/`。

搬的都是**状态与判定**，不含 HTTP：
- 心跳与关闭信号的时间戳（`beat` / `closing`）
- 后台任务计数（`busy`）——同步宣讲会时不许把程序退掉
- 「已经触发过退出」的闩锁（`done`）
- 优雅退出的钩子（`_SHUTDOWN`）
- 外壳控制端口（只用来分辨「窗口那个页面」）
- 看门狗循环本身

【搬的时候一个字没改行为】判定顺序、超时常量、宽限期、来源校验，全部照旧。

【规矩】
- 不 import `server`。
- 时间用 `time.time()`（原来就是），不引入别的时钟源。
- 这里的可变状态**是进程内单例**（跟原来一样）——自检脚本要改它就直接改这个模块的属性：
      from services import lifecycle
      lifecycle.SHELL_PORT = 4711
      lifecycle.ALIVE["beat"] = time.time()
  （`server.py` 里的 `SHELL_PORT` / `_ALIVE` / `_SHUTDOWN` 只是**同一个对象的别名**，
    改任一边都生效 —— 见 server.py 那边的注释。）
"""

import threading
import time

# 连心跳都断了这么久 -> 认为没有窗口了，可以退出（正常情况每 20 秒一次心跳）
HEARTBEAT_TIMEOUT = 300.0
# 收到「页面正在关闭」后先等这么久；期间有新心跳就取消退出（刷新页面就是这种情况）
CLOSE_GRACE_SECONDS = 15.0

# 外壳控制端口：由 launcher.py 用 --shell-port 传进来（就是写进页面地址的 shport）。
# 用途只有一个——分辨「窗口那个页面」和「别的页面」：只有窗口页面发的关闭信号才算数。
# 0 表示没有外壳（浏览器外壳模式，或直接 python server.py），这时不校验来源。
SHELL_PORT = 0

ALIVE = {
    "beat": 0.0,       # 最近一次心跳时间
    "closing": 0.0,    # 最近一次「页面要关了」的时间
    "busy": 0,         # 正在跑的后台任务数（例如同步宣讲会），>0 时不退出
    "done": False,     # 已经触发过退出，避免重复触发
}

# 优雅退出的钩子：main() 把 server.shutdown 装进来
SHUTDOWN = {"fn": None}
STATE_LOCK = threading.RLock()


def set_shell_port(port):
    """启动器用 --shell-port 告诉本服务的外壳控制端口（0 = 不校验来源）。"""
    global SHELL_PORT
    SHELL_PORT = int(port or 0)


def attach_window(source_port):
    """原子接管仍可用的后端；已经决定退出的后端绝不复用。"""
    global SHELL_PORT
    with STATE_LOCK:
        if ALIVE["done"] or not 0 < source_port < 65536:
            return False
        SHELL_PORT = source_port
        ALIVE["beat"] = time.time()
        ALIVE["closing"] = 0.0
        return True


def touch_heartbeat(source_port=None):
    """页面还开着：记一次心跳，同时取消「即将退出」的判定。"""
    with STATE_LOCK:
        if ALIVE["done"] or (SHELL_PORT and source_port != SHELL_PORT):
            return False
        ALIVE["beat"] = time.time()
        ALIVE["closing"] = 0.0
        return True


def touch_closing(source_port=None):
    """
    页面正在关闭（beforeunload）。先记下来，等宽限期过后再决定要不要退出。

    只认**窗口那个页面**发的信号：启动器开窗口时把外壳控制端口写进地址（&shport=），
    又通过 --shell-port 把同一个端口告诉本服务；页面关闭时会把它带回来。
    别的页面（浏览器里打开 127.0.0.1:8768 瞄一眼、无界面浏览器抓页面做自检）不带这个端口，
    一律忽略——否则它们一关，就会把正在用的程序在 15 秒后带走（2026-09-22 踩过：
    界面自检和无界面抓取都会触发，心跳 20 秒 > 宽限 15 秒，正好卡在竞态上）。
    SHELL_PORT == 0 时不校验（浏览器外壳模式 / 直接跑 server.py），保持老行为。
    返回 True 表示这次关闭信号被接受了。
    """
    with STATE_LOCK:
        if ALIVE["done"] or (SHELL_PORT and source_port != SHELL_PORT):
            return False
        ALIVE["closing"] = time.time()
        return True


def add_busy(delta):
    """后台任务计数：同步宣讲会这类活儿在跑的时候，不要因为窗口关了就把进程杀掉。"""
    with STATE_LOCK:
        ALIVE["busy"] = max(0, ALIVE["busy"] + delta)


def request_shutdown():
    """优雅退出：让 serve_forever() 返回，主线程收尾。"""
    with STATE_LOCK:
        ALIVE["done"] = True
    hook = SHUTDOWN.get("fn")
    if hook:
        try:
            hook()
        except Exception:
            pass


def desktop_watchdog(desktop_mode=True):
    """
    桌面模式专用的小看门狗（普通网页版不会启动它）。

    判定顺序：
      1. 有后台任务在跑 -> 不退出（别把同步到一半的活儿掐了）；
      2. 收到过「页面正在关闭」，且过了宽限期还没有新心跳 -> 退出（正常关窗口）；
      3. 心跳干脆断了很久 -> 也退出（例如浏览器崩了）。
    """
    if not desktop_mode:
        return
    while not ALIVE["done"]:
        time.sleep(2.0)
        should_exit = False
        with STATE_LOCK:
            now = time.time()
            if ALIVE["busy"] > 0:
                continue
            beat = ALIVE["beat"] or now
            closing = ALIVE["closing"]
            if (closing and (now - closing) >= CLOSE_GRACE_SECONDS
                    and (now - beat) >= CLOSE_GRACE_SECONDS) or (now - beat) >= HEARTBEAT_TIMEOUT:
                ALIVE["done"] = True
                should_exit = True
        if should_exit:
            request_shutdown()


def start_watchdog_thread(desktop_mode=True):
    """起看门狗线程（daemon），返回线程对象。"""
    thread = threading.Thread(target=desktop_watchdog, args=(desktop_mode,), daemon=True)
    thread.start()
    return thread
