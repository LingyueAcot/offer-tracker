# -*- coding: utf-8 -*-
"""
winenv.py —— Windows 环境小工具：**DPI 感知** + 显示器几何（唯一来源）

【为什么单独一个文件】
2026-09-27，那台**双屏 + 缩放**的机器上报：窗口启动后「被拉长」、约 1/5 溢出到副屏，
而且自绘标题栏的最小化 / 最大化 / 拖动全都点不动。根因是 DPI 上的一处**两头各算一次**：

  · pywebview 的 WinForms 后端 —— `start()` 里才调 `SetProcessDPIAware()`
    （`webview/platforms/winforms.py:820`），也就是说**进程是建窗口那一刻才变成 DPI 感知的**；
    而它把 `create_window()` 收到的 width/height/x/y **当逻辑像素**，再乘一次
    `GetDpiForWindow(hwnd)/96`（同文件 198-220 行）。
  · 我们这边 —— `window_state.json` 里的坐标是 `GetWindowRect` 读出来的**物理像素**，
    而 `startup_geometry()` / `position_is_visible()` 跑在「还不感知」的时候，
    那时 `GetSystemMetrics()` 给的是**虚拟化坐标**。

两边一比：缩放 150% 的屏上，记下来的 1400x900 会被 pywebview 再乘 1.5，
变成 2100x1350 物理像素 —— 比 1920 宽的屏还大，于是右下各溢出约 1/5（"拉长 + 跨屏"），
自绘标题栏在窗口右上角 → 按钮落到副屏上（"点不到"），
拖动按 CSS 像素走、物理上只走 1/1.5（"拖不动"）。这台电脑 100% 缩放 + 单屏，永远复现不了。

【这个文件给两件事】
  1. `enable_dpi_awareness()` —— 声明「逐显示器 DPI 感知」，**必须在读任何屏幕/窗口尺寸之前**调。
     Windows 只认第一次生效的那个调用，所以它必须早于 pywebview 的 `SetProcessDPIAware()`。
  2. `monitor_work_area()` / `fit_to_work_area()` / `to_logical()` —— 逐显示器的可用区域与缩放、
     「把想要的窗口矩形夹进可用区域」这条**纯计算**（能单独测）、以及物理↔逻辑像素换算。

【规矩】
- 只用标准库（ctypes）。不 import 项目里任何模块 —— 它是叶子，谁都能用。
- **任何函数都不许抛异常**：这些都是"为了排查问题"的辅助信息，读不到就回落默认值。
  （诊断报告那边尤其：采集环境信息不能成为新的崩溃点。）
- 结构体一律先声明 `argtypes`/`restype` 再调 —— 64 位下句柄不声明会被截断成 32 位
  （launcher.py 里那个 `handle()` 包装就是为这件事存在的）。
"""
import ctypes
import ctypes.wintypes as wt

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4（Windows 10 1703+）
PER_MONITOR_AWARE_V2 = -4
MONITOR_DEFAULTTONEAREST = 2
MDT_EFFECTIVE_DPI = 0

# GetSystemMetrics 的几个索引（写数字太容易记混，给它们名字）
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CMONITORS = 80

_AWARENESS_NAMES = {
    0: "UNAWARE（会被 Windows 虚拟化）",
    1: "SYSTEM_AWARE",
    # ⚠️ V1 和 V2 系统 API 都报成这个值（V2 是"上下文"的变体，级别本身一样），
    # 所以日志里出现「声明 PER_MONITOR_AWARE_V2、实际 PER_MONITOR_AWARE」是正常的。
    2: "PER_MONITOR_AWARE（V1/V2 都报这个）",
    3: "PER_MONITOR_AWARE_V2",
}


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
    ]


def enable_dpi_awareness():
    """
    把本进程声明成「逐显示器 DPI 感知」。**越早调越好**（建任何窗口之前）。

    顺序从精确到粗糙逐级回落：PER_MONITOR_AWARE_V2 -> PER_MONITOR_AWARE -> SYSTEM_AWARE。
    Windows 只认第一次成功的调用（之后调别的不会降级也不会升级），
    所以已经由别的库设过时，这里会返回当时的真实级别。
    """
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wt.BOOL
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(PER_MONITOR_AWARE_V2)):
            return "PER_MONITOR_AWARE_V2"
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "PER_MONITOR_AWARE"
    except Exception:
        pass
    try:
        if user32.SetProcessDPIAware():
            return "SYSTEM_AWARE"
    except Exception:
        pass
    return awareness_name()


def awareness_name():
    """现在实际是什么级别（给日志和诊断报告看的事实，不是我们的意图）。"""
    user32 = ctypes.windll.user32
    try:
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        value = user32.GetAwarenessFromDpiAwarenessContext(
            user32.GetThreadDpiAwarenessContext())
        return _AWARENESS_NAMES.get(value, "未知(%s)" % value)
    except Exception:
        pass
    try:
        value = ctypes.c_int()
        if ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value)) == 0:
            return _AWARENESS_NAMES.get(value.value, "未知(%s)" % value.value)
    except Exception:
        pass
    return "未知"


def monitor_scale(hmon=None):
    """这台显示器的缩放倍数（1.0 = 100%）。问不到显示器就问系统，再问不到回 1.0。"""
    try:
        if hmon:
            dx, dy = ctypes.c_uint(), ctypes.c_uint()
            ctypes.windll.shcore.GetDpiForMonitor.argtypes = [
                ctypes.c_void_p, ctypes.c_int,
                ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
            if ctypes.windll.shcore.GetDpiForMonitor(
                    ctypes.c_void_p(hmon), MDT_EFFECTIVE_DPI,
                    ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value:
                return round(dx.value / 96.0, 4)
    except Exception:
        pass
    dpi = system_dpi()
    return round(dpi / 96.0, 4) if dpi else 1.0


def system_dpi():
    """系统 DPI（96 = 100%）。⚠️ 进程**不感知**时这个值恒为 96 —— 所以要先 enable。"""
    try:
        return int(ctypes.windll.user32.GetDpiForSystem()) or 0
    except Exception:
        return 0


def monitor_work_area(x=None, y=None):
    """
    (x, y) 所在（或离它最近）那台显示器的**可用区域**和缩放倍数：
    返回 `((left, top, right, bottom), scale)`，全部是**物理像素**（前提：已声明 DPI 感知）。
    取不到就回落成「主屏整块 + 1.0」—— 这个函数只决定"窗口摆哪儿"，不许把启动弄挂。
    """
    user32 = ctypes.windll.user32
    work = None
    scale = 1.0
    try:
        user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
        user32.GetMonitorInfoW.restype = wt.BOOL
        hmon = user32.MonitorFromPoint(
            wt.POINT(int(x or 0), int(y or 0)), MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            work = (info.rcWork.left, info.rcWork.top,
                    info.rcWork.right, info.rcWork.bottom)
            scale = monitor_scale(hmon)
    except Exception:
        work = None
    if work is None:
        try:
            work = (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
        except Exception:
            work = (0, 0, 1920, 1080)
    return work, scale


def fit_to_work_area(x, y, width, height, work, min_width=900, min_height=640):
    """
    **纯计算**：把「想要的窗口矩形」夹进 `work=(left, top, right, bottom)`，返回 `(x, y, w, h)`。

    为什么要夹：缩放/多屏机器上，上次记下来的尺寸可能来自另一块更大的屏或另一次缩放，
    直接照用就会出现「窗口比屏幕还大、右下溢到副屏」。
    `x`/`y` 传 None = 这次没有可用的历史位置，就在这块屏上居中。

    全部按物理像素算，不碰任何 API —— 所以能单独写成用例（tools/test_window_state.py）。
    """
    left, top, right, bottom = (int(v) for v in work)
    avail_w = max(int(min_width), right - left)
    avail_h = max(int(min_height), bottom - top)
    width = max(int(min_width), min(int(width or 0), avail_w))
    height = max(int(min_height), min(int(height or 0), avail_h))
    if x is None or y is None:
        return (left + (avail_w - width) // 2, top + (avail_h - height) // 2, width, height)
    x, y = int(x), int(y)
    # 先保证右下不越界，再保证左上不越界（后者优先：标题栏必须在屏幕里，不然拖都拖不回来）
    x = min(max(x, left), max(left, right - width))
    y = min(max(y, top), max(top, bottom - height))
    return x, y, width, height


def to_logical(value, scale):
    """
    物理像素 -> **逻辑像素**。

    为什么需要：pywebview 把 `create_window()` 的宽高/坐标当逻辑像素，建窗口时会再乘一次
    那台显示器的缩放（`winforms.py:198-220`）。我们要的是「物理上正好这么大」，
    所以传进去之前先除掉缩放；窗口建好之后还会用 SetWindowPos 按物理像素钉一次。
    """
    scale = float(scale or 1.0)
    return max(1, int(round(int(value) / scale)))


def screen_size():
    """主屏尺寸（已声明 DPI 感知时是物理像素）。"""
    try:
        user32 = ctypes.windll.user32
        return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
    except Exception:
        return 0, 0


def virtual_screen():
    """所有显示器拼起来的虚拟桌面：(left, top, width, height)。副屏在主屏左边时 left 是负的。"""
    try:
        user32 = ctypes.windll.user32
        return (int(user32.GetSystemMetrics(76)), int(user32.GetSystemMetrics(77)),
                int(user32.GetSystemMetrics(78)), int(user32.GetSystemMetrics(79)))
    except Exception:
        return 0, 0, 0, 0


def display_count():
    """接了几台显示器（GetSystemMetrics(80) = SM_CMONITORS）。"""
    try:
        return int(ctypes.windll.user32.GetSystemMetrics(80))
    except Exception:
        return 0
