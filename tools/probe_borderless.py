# -*- coding: utf-8 -*-
"""
无边框窗口探测（阶段 3 开工前的一次性检查）。

【为什么要这个】
「无边框」不是加一个参数就完了。窗口一旦去掉系统标题栏，会**一起丢掉**这些本来
免费的能力：拖动窗口、鼠标拉边框改大小、四角圆角、投影阴影、双击最大化。
这些要靠 Win32 一点一点补回来，而补法在不同环境下不一定都成立。

写启动器的人（我）没法在这台机器上跑 GUI，所以先做个带说明的测试窗口：
你按屏幕上的清单动手试几下，把结果告诉我，我再决定用哪套补法。

【怎么用】
直接双击 work\\probe_borderless.cmd 就行。
窗口会弹出来，屏幕上直接写着该试什么、以及 Win32 那几步成没成。
试完点窗口里的「关掉这个测试窗口」。

结果同时写进 work\\borderless_probe.txt。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time
import traceback

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "borderless_probe.txt")

LINES = []

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>无边框测试</title>
<style>
  *{box-sizing:border-box}
  body{
    margin:0; font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
    background:#f4f5fa; color:#1b2333; font-size:14px; line-height:1.6;
    user-select:text; -webkit-font-smoothing:antialiased;
  }
  .wrap{padding:22px 26px 30px}
  h1{margin:0 0 4px; font-size:19px}
  .sub{color:#6b7488; font-size:12.5px; margin-bottom:16px}
  h2{margin:18px 0 8px; font-size:14px}
  ol{margin:0; padding-left:22px}
  li{margin-bottom:9px}
  .k{font-weight:700; color:#4f46e5}
  pre{
    margin:8px 0 0; padding:12px 14px; background:#fff; border:1px solid #e4e6ef;
    border-radius:10px; font-family:Consolas,monospace; font-size:12px;
    white-space:pre-wrap; word-break:break-all; color:#3b4252;
  }
  .box{background:#fff; border:1px solid #e4e6ef; border-radius:12px; padding:14px 16px}
  .warn{background:#fdf3e3; border-color:#f0d9a8}
  button{
    margin-top:18px; padding:9px 18px; font-size:14px; font-family:inherit;
    border:0; border-radius:9px; background:#4f46e5; color:#fff; cursor:pointer;
  }
  button:hover{background:#4338ca}
</style></head>
<body><div class="wrap">
  <h1>无边框窗口测试</h1>
  <div class="sub">这个窗口去掉了系统标题栏。请按下面 4 条动手试一下，然后告诉 AI 结果。</div>

  <div class="box">
    <h2 style="margin-top:0">请你试这 4 件事</h2>
    <ol>
      <li>按住窗口里<span class="k">任意空白处</span>拖动 → 窗口会跟着动吗？</li>
      <li>把鼠标移到窗口<span class="k">最边缘</span>（上下左右、四个角）再拖 → 能改变窗口大小吗？</li>
      <li><span class="k">双击</span>窗口顶部的空白处 → 会最大化 / 还原吗？</li>
      <li>看窗口的四个角：是<span class="k">圆角</span>还是直角？周围有没有<span class="k">阴影</span>？</li>
    </ol>
  </div>

  <h2>机器自己测出来的结果</h2>
  <pre id="win32">正在检测…</pre>

  <button id="bye">关掉这个测试窗口</button>
</div>
<script>
  document.getElementById("bye").onclick = function () {
    // 用 pywebview 的桥关窗口；没有桥就直接报错给用户看
    try { window.pywebview.api.close_window(); }
    catch (e) { document.getElementById("bye").textContent = "关不掉，直接点窗口 X 或按 Alt+F4"; }
  };
</script>
</body></html>"""


def add(text=""):
    LINES.append(str(text))


def flush():
    try:
        with open(OUT, "w", encoding="utf-8") as handle:
            handle.write("\n".join(LINES))
    except Exception:
        pass
    try:
        print("\n".join(LINES))
    except Exception:
        pass


class Api(object):
    """给测试页面用的最小接口：关窗口。"""

    def close_window(self):
        try:
            import webview
            if webview.windows:
                webview.windows[0].destroy()
        except Exception:
            pass
        return True


def get_style(hwnd, index):
    user32 = ctypes.windll.user32
    try:
        return user32.GetWindowLongPtrW(hwnd, index)
    except Exception:
        return user32.GetWindowLongW(hwnd, index)


def set_style(hwnd, index, value):
    user32 = ctypes.windll.user32
    try:
        return user32.SetWindowLongPtrW(hwnd, index, value)
    except Exception:
        return user32.SetWindowLongW(hwnd, index, value)


def describe_style(style):
    names = [
        (0x00C00000, "WS_CAPTION"),
        (0x00040000, "WS_THICKFRAME"),
        (0x00020000, "WS_MINIMIZEBOX"),
        (0x00010000, "WS_MAXIMIZEBOX"),
        (0x00080000, "WS_SYSMENU"),
        (0x80000000, "WS_POPUP"),
        (0x10000000, "WS_VISIBLE"),
    ]
    return ", ".join(name for bit, name in names if style & bit) or "(无)"


def probe_window():
    """窗口建好之后，量一遍、补一遍、再量一遍。"""
    time.sleep(2.5)
    report = []
    try:
        import webview
        window = webview.windows[0]
    except Exception:
        add("取窗口失败：%s" % traceback.format_exc())
        return

    try:
        hwnd = int(window.native.Handle.ToInt64())
    except Exception as exc:
        add("拿不到窗口句柄：%r" % exc)
        report.append("拿不到窗口句柄：%r" % exc)
        try:
            window.evaluate_js("document.getElementById('win32').innerText = %s;"
                               % json.dumps("\n".join(report)))
        except Exception:
            pass
        return

    add("窗口句柄 HWND: %s" % hwnd)
    report.append("HWND: %s" % hwnd)

    user32 = ctypes.windll.user32
    dwm = ctypes.windll.dwmapi

    before = get_style(hwnd, GWL_STYLE)
    ex_before = get_style(hwnd, GWL_EXSTYLE)
    add("改之前的窗口样式: 0x%08X  (%s)" % (before & 0xFFFFFFFF, describe_style(before)))
    add("改之前扩展样式    : 0x%08X" % (ex_before & 0xFFFFFFFF))
    report.append("pywebview 建出来的无边框窗口样式：\n  %s" % describe_style(before))

    # ---- 补回「能用鼠标拉边框改大小」和「系统投影」 ----
    want = before | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU
    set_style(hwnd, GWL_STYLE, want)
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
    time.sleep(0.3)
    after = get_style(hwnd, GWL_STYLE)
    add("改之后的窗口样式: 0x%08X  (%s)" % (after & 0xFFFFFFFF, describe_style(after)))
    got_thick = bool(after & WS_THICKFRAME)
    add("WS_THICKFRAME 加上了吗 : %s" % ("是" if got_thick else "否 —— 被 WinForms 改回去了"))
    report.append("加 WS_THICKFRAME（用来支持鼠标拉边框）：%s"
                  % ("成功" if got_thick else "失败，被改回去了"))

    # ---- 圆角 ----
    corner = ctypes.c_int(DWMWCP_ROUND)
    try:
        hr = dwm.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                                       ctypes.byref(corner), ctypes.sizeof(corner))
        add("DWM 圆角（属性 33）: hr=%d %s" % (hr, "（成功）" if hr == 0 else "（失败）"))
        report.append("DWM 圆角：%s" % ("成功" if hr == 0 else "调用失败 hr=%d" % hr))
    except Exception as exc:
        add("DWM 圆角调用异常：%r" % exc)
        report.append("DWM 圆角：调用异常 %r" % exc)

    # ---- 客户区尺寸变化（判断是否真的多了边框） ----
    try:
        wrect = wt.RECT()
        crect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wrect))
        user32.GetClientRect(hwnd, ctypes.byref(crect))
        add("窗口矩形: %d,%d - %d,%d" % (wrect.left, wrect.top, wrect.right, wrect.bottom))
        add("客户区    : 0,0 - %d,%d   （差值 = 系统边框厚度）" % (crect.right, crect.bottom))
    except Exception as exc:
        add("量尺寸失败：%r" % exc)

    add("")
    add(">>> 窗口已弹出。请按窗口里写的 4 条动手试，然后关掉它。")

    try:
        window.evaluate_js("document.getElementById('win32').innerText = %s;"
                           % json.dumps("\n".join(report)))
    except Exception:
        add("把结果写进页面失败（不影响结论）")


def main():
    add("无边框窗口探测")
    add("时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    add("Python: %s" % sys.version.split()[0])
    add("")
    try:
        import webview
    except Exception:
        add("导入 pywebview 失败：")
        add(traceback.format_exc())
        flush()
        return 1

    add("pywebview 导入成功")
    add("建一个无边框窗口 900x620（easy_drag=True，方便你直接拖）…")

    threading.Thread(target=probe_window, daemon=True).start()
    try:
        webview.create_window("无边框测试", html=PAGE, width=900, height=620,
                              frameless=True, easy_drag=True, text_select=True,
                              background_color="#f4f5fa", js_api=Api())
        webview.start(private_mode=True)
    except Exception:
        add("建窗口失败：")
        add(traceback.format_exc())
        flush()
        return 1

    add("")
    add("窗口已关闭。")
    add("请把这几条的答案告诉 AI：")
    add("  1) 拖空白处，窗口动不动？")
    add("  2) 拉最边缘，能不能改大小？")
    add("  3) 双击顶部，会不会最大化/还原？")
    add("  4) 四角是圆的还是直角？有没有阴影？")
    flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
