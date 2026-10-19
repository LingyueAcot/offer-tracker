# -*- coding: utf-8 -*-
"""
量一量「顶部黑框」到底是什么（先打开程序，再双击 measure_window_frame.cmd）。

【为什么要它】
黑框这件事已经猜错两次了（先怀疑 easy_drag，又怀疑「一开始就最大化」），
不能再靠猜。这个脚本直接量，而且**当场试几种修法**，看哪种能让黑条消失：

  1. 量窗口尺寸 vs 客户区尺寸（差值 = 系统额外画的那圈边框有多厚）
  2. 抓窗口那一片，逐行读顶部像素：黑条有多高、什么颜色
  3. 正常大小 / 最大化 各量一次（脚本自己会最大化、量完还原）
  4. **试修**：依次改 DWM 边框颜色、重算窗口边框，每改一样就重量一次，
     看黑条有没有消失。这样一次运行就能确定「原因 + 有效的修法」。

结果写进 work\\window_frame.txt。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

TITLE = "应届海投管理"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "window_frame.txt")

SRCCOPY = 0x00CC0020
SW_MAXIMIZE = 3
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
DWMWA_BORDER_COLOR = 34
DWMWA_COLOR_NONE = 0xFFFFFFFE

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

LINES = []


def add(text=""):
    LINES.append(str(text))


def flush():
    try:
        with open(OUT, "w", encoding="utf-8") as handle:
            handle.write("\n".join(LINES))
    except Exception as exc:
        add("写文件失败：%r" % (exc,))
    try:
        print("\n".join(LINES))
    except Exception:
        pass


def grab(hwnd):
    """把窗口那一片抓到内存位图中，返回 (宽, 高, 取色函数, 收尾函数, 窗口矩形)。"""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = max(1, rect.right - rect.left)
    h = max(1, rect.bottom - rect.top)
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, rect.left, rect.top, SRCCOPY)

    def pixel(x, y):
        value = gdi32.GetPixel(hdc_mem, int(x), int(y))
        if value == 0xFFFFFFFF:      # CLR_INVALID
            return None
        return (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)

    def cleanup():
        try:
            gdi32.SelectObject(hdc_mem, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(0, hdc_screen)
        except Exception:
            pass

    return w, h, pixel, cleanup, rect


def is_dark(rgb):
    return bool(rgb) and (rgb[0] + rgb[1] + rgb[2]) < 150


def dark_band_height(pixel, w, h):
    """从最顶上往下数，连续是暗色的高度（就是「黑条」大概有多高）。"""
    mid = max(1, w // 2)
    height = 0
    for y in range(0, min(h, 200)):
        if is_dark(pixel(mid, y)):
            height = y + 1
        elif y > 2:
            break
    return height


def band_now(hwnd):
    """现在顶部那条暗色有多高（-1 表示抓图失败）。"""
    try:
        w, h, pixel, cleanup, _rect = grab(hwnd)
    except Exception:
        return -1
    try:
        return dark_band_height(pixel, w, h)
    finally:
        cleanup()


def window_styles(hwnd):
    """把当前窗口样式读出来（看它到底带着哪些位）。"""
    try:
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        style = user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), -16)
        ex = user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), -20)
        return "0x%08X / 扩展 0x%08X" % (style & 0xFFFFFFFF, ex & 0xFFFFFFFF)
    except Exception as exc:
        return "读不到：%r" % (exc,)


def measure(label, hwnd):
    add("=" * 62)
    add(label)
    add("=" * 62)
    add("窗口样式   : %s" % window_styles(hwnd))
    crect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(crect))
    try:
        w, h, pixel, cleanup, rect = grab(hwnd)
    except Exception as exc:
        add("抓图失败：%r" % (exc,))
        return
    try:
        add("窗口矩形   : %d,%d - %d,%d" % (rect.left, rect.top, rect.right, rect.bottom))
        add("窗口尺寸   : %d x %d" % (w, h))
        add("客户区尺寸 : %d x %d" % (crect.right, crect.bottom))
        add("四周被系统占掉: %d 像素（横）x %d 像素（竖）"
            % (w - crect.right, h - crect.bottom))
        add("")

        mid = max(1, w // 2)
        add("顶部正中那一列（x=%d）的像素颜色：" % mid)
        ys = [0, 1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 26, 32, 40, 48, 56, 64]
        for y in ys:
            if y >= h:
                break
            rgb = pixel(mid, y)
            mark = "  ← 暗色" if is_dark(rgb) else ""
            add("   y=%-3d %s%s" % (y, rgb, mark))

        # 连续暗色的高度，就是「黑框」大概有多高
        dark_h = 0
        for y in range(0, min(h, 200)):
            if is_dark(pixel(mid, y)):
                dark_h = y + 1
            elif y > 2:
                break
        add("")
        add("顶部连续暗色高度: %d 像素" % dark_h)

        add("")
        add("左边那一行（y=%d）的像素颜色：" % (h // 2))
        for x in [0, 1, 2, 3, 4, 6, 8, 12]:
            if x >= w:
                break
            rgb = pixel(x, h // 2)
            mark = "  ← 暗色" if is_dark(rgb) else ""
            add("   x=%-3d %s%s" % (x, rgb, mark))
    finally:
        cleanup()
    add("")


def looks_occluded(pixel, w, h):
    """
    自检：这次抓到的到底是不是程序窗口？
    在画面里取几个分散的点，如果颜色全都差不多（一整块纯色），
    那多半是抓到了盖在上面的别的窗口 —— 前两版就是这么错的。
    """
    samples = []
    for fx in (0.25, 0.5, 0.75):
        for fy in (0.25, 0.5, 0.75):
            rgb = pixel(int(w * fx), int(h * fy))
            if rgb:
                samples.append(rgb)
    if len(samples) < 4:
        return True, "取色失败"
    spread = max(sum(c) for c in samples) - min(sum(c) for c in samples)
    if spread < 12:
        return True, "整片都是同一个颜色 %s，多半抓到了别的窗口" % (samples[0],)
    return False, "颜色有变化，看着确实是程序窗口"


def main():
    add("窗口黑框测量")
    add("时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    add("")
    hwnd = user32.FindWindowW(None, TITLE)
    if not hwnd:
        add("没找到标题为「%s」的窗口。" % TITLE)
        add("请先把程序打开（双击桌面图标），等界面出来之后再跑这个脚本。")
        flush()
        return 1
    add("找到窗口，句柄 %s" % hwnd)
    add("")
    add(">>> 接下来请这样做：")
    add("    1) 点一下程序窗口，让它到最前面")
    add("    2) 回到这个黑窗口，按回车")
    add("    按完之后别用鼠标去点程序窗口以外的地方，脚本自己会把它提到最前。")
    add("")
    try:
        input("按回车开始测量 > ")
    except Exception:
        pass

    # 关键：先把程序窗口提到最前面，否则抓到的会是这个命令行窗口
    #（前两版就是栽在这里）
    try:
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    except Exception:
        pass
    time.sleep(1.2)

    # 这一版**不改变窗口大小**：只有正常大小的时候，那圈边框才在屏幕上看得见。
    # 上一版改成全程最大化，反而把边框推出了屏幕，实验等于白做。
    measure("正常大小（没有改变窗口大小）", hwnd)

    try:
        w, h, pixel, cleanup, _r = grab(hwnd)
        try:
            occluded, why = looks_occluded(pixel, w, h)
        finally:
            cleanup()
        add("遮挡自检：%s%s" % ("疑似被挡住 —— " if occluded else "", why))
        if occluded:
            add("")
            add("!! 这次的数据不可信：多半又抓到别的窗口了。")
            add("!! 请重新跑一次，并且严格按上面的两步做（先点程序窗口，再按回车）。")
    except Exception as exc:
        add("遮挡自检失败：%r" % (exc,))

    # ---------- 分辨：那圈黑边是谁画的 ----------
    add("")
    add("=" * 62)
    add("分辨那圈黑边是谁画的")
    add("=" * 62)
    before = band_now(hwnd)
    add("  起点                              顶部暗色高度 = %d 像素" % before)
    add("")
    add("  把窗口边框颜色临时改成亮绿色：如果那圈边变绿 -> 是 Windows/DWM 画的；")
    add("  如果还是黑的 -> 是窗口自己不画（WinForms）留下的没上色的区域。")

    # 试修一：把 DWM 边框颜色设成亮绿（0x0000FF00 在 COLORREF 里是 BGR，纯绿）
    try:
        value = ctypes.c_uint(0x0000FF00)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_BORDER_COLOR, ctypes.byref(value), ctypes.sizeof(value))
        time.sleep(0.7)
        after = band_now(hwnd)
        add("")
        add("  改名后 hr=%d" % hr)
        try:
            w, h, pixel, cleanup, _r = grab(hwnd)
            try:
                add("  y=0/3/6 的颜色: %s %s %s"
                    % (pixel(w // 2, 0), pixel(w // 2, 3), pixel(w // 2, 6)))
                add("  x=0/3/6 的颜色: %s %s %s"
                    % (pixel(0, h // 2), pixel(3, h // 2), pixel(6, h // 2)))
            finally:
                cleanup()
        except Exception as exc:
            add("  取色失败：%r" % (exc,))
        add("  顶部暗色高度 = %d 像素" % after)
    except Exception as exc:
        add("  试修一失败：%r" % (exc,))

    # 试修二：把 DWM 边框设成「无」，看那圈黑边会不会真的消失
    try:
        value = ctypes.c_uint(DWMWA_COLOR_NONE)
        hr2 = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_BORDER_COLOR, ctypes.byref(value), ctypes.sizeof(value))
        time.sleep(0.7)
        after2 = band_now(hwnd)
        add("")
        add("  再设成「无」(hr=%d) -> 顶部暗色高度 = %d 像素%s"
            % (hr2, after2, "   <== 有效" if after2 < 7 else ""))
        try:
            w, h, pixel, cleanup, _r = grab(hwnd)
            try:
                add("  y=0/3/6 的颜色: %s %s %s"
                    % (pixel(w // 2, 0), pixel(w // 2, 3), pixel(w // 2, 6)))
            finally:
                cleanup()
        except Exception as exc:
            add("  取色失败：%r" % (exc,))
    except Exception as exc:
        add("  试修二失败：%r" % (exc,))

    add("")
    add("把 work\\window_frame.txt 给 AI 看就行 —— 里面有确切的像素值。")
    flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
