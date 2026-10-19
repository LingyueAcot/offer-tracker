# -*- coding: utf-8 -*-
"""
应届海投管理 · 桌面启动器（v3）
=====================================================================

【和 2.0 的启动器有什么不同】
   2.0 是「起服务 -> 调 Edge 的 --app 模式开窗口」。那个窗口的标题栏是浏览器画的，
   颜色跟着系统深色模式走，改不了；窗口本质上也还是一个浏览器进程。

   v3 用 **pywebview + WebView2** 自己建窗口。好处：
     - 窗口是自己的（标题栏、图标、任务栏条目都是「应届海投管理」）
     - 以后可以做成无边框 + 自绘标题栏（阶段 3、4）
     - 启动时先显示一张自己画的启动画面，不会白屏
     - 窗口大小位置能记住

   这一版（阶段 2）**故意还带系统标题栏**，先把外壳换掉、风险降到最低；
   无边框要补的那堆窗口行为（拖动、8 向缩放）留到阶段 3。

【退出链路（和 2.0 保持一致）】
   关窗口 -> 本启动器给服务发一个「页面正在关闭」信号 -> 服务自己判断能不能退
   （后台正在同步宣讲会时不会退，会等活干完）-> 服务退出 -> 启动器退出。

【config.json 里的开关】
   "shell": "pywebview"  用新外壳（默认）
   "shell": "browser"    退回 2.0 的 Edge --app 方式，万一新外壳出问题可以应急

【为什么指定 Python 版本】
   程序实际要跑在 3.12 上（pywebview 装在 3.12 里）。
   以前这台机器的 `python` 指向 Anaconda 3.7，所以这里**不依赖 PATH**、主动去找
   这台电脑最新版本的 Python（现在 PATH 里也是 3.12 了，但这条规则不变）。
   也可以在 config.json 的 "python" 里写死一个路径覆盖。

【怎么被启动】
   桌面快捷方式 -> pythonw.exe launcher.py（工作目录 = 本文件夹）
   想看报错就用「启动（调试窗口）.cmd」，那个会保留命令行输出。
   新外壳的报错也会写进 logs/launcher.log。
"""
from __future__ import print_function

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback

import winenv  # 同目录的「DPI 感知 + 显示器几何」（2026-09-27 新增），说明见 winenv.py 顶部
import paths as app_paths  # 同目录的「路径契约」（2026-10-11 加）：程序/数据/日志/缓存各在哪，说明见 paths.py

# ⚠️ 路径**不要**在这里拼 `BASE_DIR + "data"`：全部走 `app_paths.*`（唯一决定处）。
#    这样子目录/新装能各归各位，自检脚本也能用 RESUME_TRACKER_DATA_DIR 把 I/O 圈进临时目录。
BASE_DIR = app_paths.program_dir()
CONFIG_PATH = app_paths.config_path()
RUNTIME_PATH = app_paths.runtime_path()
STATE_PATH = app_paths.window_state_path()
LOG_DIR = app_paths.logs_dir()
LOG_PATH = app_paths.launcher_log_path()
# 服务端的输出（print + 未捕获异常的 traceback）接在这里 —— 见 start_server() 里的说明。
# 2026-09-25 加：在此之前服务端输出是直接丢掉的，等于服务端崩了没有任何线索。
SERVER_LOG_PATH = app_paths.server_log_path()
SPLASH_PATH = os.path.join(app_paths.web_dir(), "splash.html")
# 程序图标：**必须在程序内部**（web\app.ico），快捷方式的 IconLocation 和下面的
# 运行时窗口图标（apply_window_icon）都从这里拼路径去读。
# ⚠️ 绝不要改成"指向源图"（比如下载目录里那张 jpg）：源图被挪走/清掉，图标就"莫名其妙消失"了。
# 要换图：`python tools\make_app_icon.py <源图>` 重新生成这个 .ico（见 tools\make_app_icon.py 的说明）。
APP_ICON = os.path.join(app_paths.web_dir(), "app.ico")
# 桌面快捷方式的图标**不能直接用上面那份**（2026-10-10 查了一整轮才定位到）：
# 程序目录这棵树可能带着特殊的 ACL / 低完整性标签（这台电脑工作目录就被沙箱打过标），
# 而 **Explorer 取快捷方式图标时会拒绝这类位置里的图标文件** —— 表现是快捷方式永远
# 显示成一张通用图（对照实测：同一个文件原样复制到用户目录下就正常显示）。
# 所以壳用的那份单独放用户目录，由 sync_shell_icon() 每次启动自愈同步；
# **主图标仍然只在程序内部**（web\app.ico），换图只换它。
# 2026-10-11：这份的位置也交给 paths.py —— 便携副本与安装副本不再抢同一个文件（实测过 PermissionError）。
SHELL_ICON = app_paths.shell_icon_path()

# Edge --app 专用的浏览器配置目录（只有退回模式才用得上）。
# **故意放在 data 外面**：data 是你的数据，里面只该有数据库和日志；
# 浏览器缓存动辄几十上百 MB，混进去会把「复制文件夹备份」变得又慢又大。
# 2026-10-11：改放 paths 的 cache 目录（老装仍在程序隔壁，位置没变）。
BROWSER_PROFILE_DIR = app_paths.browser_profile_dir()
# WebView2 的用户数据目录。同一个道理，也放缓存目录、不进 data。
# 主题选择（存在页面的 localStorage 里）就保存在这儿，所以它必须持久化。
WEBVIEW_PROFILE_DIR = app_paths.webview_profile_dir()

# 找到的解释器缓存（进程内，只在本次运行有效）。
# find_python() 要扫 4 个根目录，实测约 0.3–0.4 秒。进程内记一份，
# 让「同一个进程里再问一次」不再重复扫盘（比如提前预热之后，worker 再取就秒回）。
# **故意不写回 config.json** —— 理由写在 find_python 的 docstring 里（换机器就失效）。
_PYTHON_CACHE = {}

# 开窗口时按顺序试：Edge 优先（Win10/11 一定自带），再是 Chrome
BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

WINDOW_TITLE = "应届海投管理"
DEFAULT_WINDOW_WIDTH = 1280
DEFAULT_WINDOW_HEIGHT = 860
MIN_WINDOW_WIDTH = 900
MIN_WINDOW_HEIGHT = 640
# 单实例用的命名互斥体。第二次双击靠它判断「已经开着了」，
# 然后只需要把已有窗口提到前面，不再开第二个。
# 自检副本可以换成独立的锁（环境变量），这样能和主程序同时开着各自测；
# 平时双击走的就是下面这个默认名字，行为完全不变。
MUTEX_NAME = (os.environ.get("RESUME_TRACKER_MUTEX")
              or "Local\\ResumeTrackerV3_SingleInstance")

SW_RESTORE = 9
SW_SHOW = 5
# RedrawWindow 的旗标：让整棵窗口树失效并马上重画（治"最小化恢复后白屏"，见 nudge_repaint）
RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_FRAME = 0x0400
RDW_ALLCHILDREN = 0x0080
RDW_UPDATENOW = 0x0100
ERROR_ALREADY_EXISTS = 183
# 上一个实例关窗之后还要收尾一两秒。这段时间里再双击，就等它一会儿，
# 不要急着弹「程序已经在运行了」——见 acquire_single_instance() 的说明。
WAIT_FOR_EXITING_INSTANCE = 6.0


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def log(message):
    """写启动日志（桌面模式没有黑框，出问题只能靠这个文件）。"""
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR)
        # 轮转只在"用户目录装法"生效（老装的日志一直是只追加、不截断，别悄悄改它的行为）
        if app_paths.rotation_enabled():
            app_paths.rotate_log(LOG_PATH)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("[%s] %s\n" % (stamp, message))
    except Exception:
        pass


def alert(message, title=WINDOW_TITLE):
    """弹一个系统提示框。用 ctypes 调 Windows 自带 API，不需要额外装东西。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
    except Exception:
        print(message)


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def python_version_key(path):
    """
    从目录名里抠出版本号用来比大小。认两种名字：
      `Python312`     —— 64 位安装（python.org 的常规命名）
      `Python312-32`  —— **32 位安装**（同一台机器上装 32 位版时，安装器给目录名加 `-32`）
    返回 `(次版本号, 位数)` —— 位数让 64 位在同版本时排前面。

    ⚠️ 2026-09-28 的 bug：原来的正则要求目录名以「Python3 + 一串数字」结尾，于是
    **32 位安装的 Python 在扫描里整个被跳过**。那台机器上唯一的候选就剩
    `Program Files` 里那个 Python39（3.9），后端被换过去 —— 而它没装 openpyxl，
    界面就报「缺少 openpyxl」；依赖其实装在启动器自己用的 3.12-32 里。
    """
    match = re.search(r"Python(\d)(\d+)(?:-(\d+))?$", os.path.dirname(path))
    if not match:
        return (0, 0)
    bits = int(match.group(3)) if match.group(3) else 64
    return (int(match.group(2)), bits)


def scan_python_candidates(roots):
    """
    在给定目录里找出所有 `Python3xx` / `Python3xx-32` 的 `pythonw.exe`。

    返回 `[(排序键, 路径)]`（键就是 python_version_key）。**只读目录，纯逻辑，能单独测。**
    为什么要单独抽出来：上面那个 `-32` 的漏网之鱼就是藏在这段扫描里，
    抽成函数之后 `tools/test_window_state.py` 能用一个假目录树把它钉住。
    """
    found = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for name in entries:
            if not re.match(r"^Python3\d+(-\d+)?$", name):
                continue
            exe = os.path.join(root, name, "pythonw.exe")
            if os.path.isfile(exe):
                found.append((python_version_key(exe), exe))
    return found


def beside_current_python():
    """
    「启动自己的那个解释器」的 pythonw 版（同目录的 pythonw.exe）。

    为什么它要排在最前面（2026-09-28）：`启动.cmd` 是把依赖（pywebview / openpyxl）
    装进**它自己选中的那个 Python** 的，而它随后就用那个 Python 拉起启动器 ——
    所以「启动器正在用的解释器」= 「装好依赖的那一个」。后端只要用同一个，
    依赖必然齐；换一个（哪怕是"更新"的版本）就很可能什么都没有，
    表现就是界面报「缺少 openpyxl」。
    """
    try:
        current = sys.executable or ""
        if not current:
            return None
        sibling = os.path.join(os.path.dirname(current), "pythonw.exe")
        if os.path.isfile(sibling):
            return sibling
        if os.path.isfile(current):
            return current
    except Exception:
        pass
    return None


def find_python(config):
    """
    找用来跑 server.py 的解释器。

    ⚠️ **顺序的第一条是「启动自己的那个解释器」**（2026-09-28 改）：依赖是
    `启动.cmd` 装进"它自己选中的那个 Python"的，而它随后就用那个 Python 拉起启动器 ——
    所以「启动器正在用的解释器」就是"装好依赖的那一个"。这条排第一，后端必然拿得到
    pywebview/openpyxl。**以前是"装了的 Python 里版本号最大的赢"**，于是在一台
    既有 Python39 又有 `Python312-32` 的机器上挑走了 3.9（还没有 `-32` 那种目录名认不出来），
    界面就报「缺少 openpyxl」。

    完整顺序：
      0. 进程内缓存（见下面 _PYTHON_CACHE 的说明）；
      1. config.json 里写死的 "python"（用户明确指定就以他为准）；
      2. **启动自己的那个解释器**（依赖装在它里面）；
      3. 这台电脑安装的 Python3xx 里版本号最大的那个（要 pythonw.exe，它没有控制台窗口）；
      4. PATH 里的 pythonw；
      5. 当前解释器本身。

    ⚠️ **结果只缓存在进程内，绝不写回 config.json。**
    写文件看似能省掉以后每次的目录扫描，但会把「这台机器的解释器路径」固化进配置：
    程序是「复制文件夹就能跑」的，同一个文件夹换台机器、或者用户重装 Python 换了版本，
    那个写死的路径就失效了 —— 虽然上面第一步会校验并自动回退，但会白等一次失败。
    缓存在进程内就没有这个问题：换个环境、换个进程，缓存自动是空的，重新探测一次，
    行为永远和「没有这张便签」时一致。
    """
    cached = _PYTHON_CACHE.get("exe")
    if cached and os.path.isfile(cached):
        return cached

    fixed = (config.get("python") or "").strip()
    if fixed:
        for name in ("pythonw.exe", "python.exe"):
            candidate = os.path.join(fixed, name) if os.path.isdir(fixed) else fixed
            if os.path.isfile(candidate):
                log("用 config.json 里指定的解释器：%s" % candidate)
                _PYTHON_CACHE["exe"] = candidate
                return candidate
        log("config 里指定的 python 路径不存在：%s" % fixed)

    own = beside_current_python()
    if own:
        log("用启动它的那个解释器（依赖就装在它里面）：%s" % own)
        _PYTHON_CACHE["exe"] = own
        return own

    roots = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python"),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local",
                     "Programs", "Python"),
    ]
    found = scan_python_candidates(roots)
    if found:
        # 键是 (次版本号, 位数)：64 位在同版本时排前面（32 位能用，但坑更多，见 AGENTS.md 第 12 条）
        found.sort(key=lambda item: item[0])
        chosen = found[-1][1]
        log("选用 Python：%s（这台电脑装了 %d 个候选）" % (chosen, len(found)))
        _PYTHON_CACHE["exe"] = chosen
        return chosen

    from shutil import which
    for name in ("pythonw.exe", "pythonw"):
        hit = which(name)
        if hit:
            _PYTHON_CACHE["exe"] = hit
            return hit
    beside = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.isfile(beside):
        _PYTHON_CACHE["exe"] = beside
        return beside
    _PYTHON_CACHE["exe"] = sys.executable
    return sys.executable


def _raw_ping(port, timeout):
    """
    用最原始的方式问一句「服务在不在」：一次 connect + 一次 recv。

    **刻意不用 urllib**。它要经过代理探测、DNS 解析、构建 opener 等一串环节，
    实测在个别机器上会把启动流程卡死（日志显示就停在这一步，窗口一直挂在启动画面）。
    这里只有一次 TCP 连接，出什么问题都只是「返回 False」。
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", port))
        sock.sendall(b"GET /api/ping HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                     b"Connection: close\r\n\r\n")
        data = sock.recv(64)
        return b" 200 " in data[:24]
    except Exception:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def ping(port, timeout=0.8, hard_timeout=4.0):
    """
    程序是不是已经在跑了？

    外面又包了一层「线程 + join(硬超时)」。原因：只靠 socket 自己的 timeout 不够保险 ——
    实测启动流程卡在第一次网络请求上过。这里给整件事一个**绝对上限**：
    到点还没结果就当「没有服务」，绝不允许它把启动流程拖住。
    """
    box = {"ok": False}

    def run():
        box["ok"] = _raw_ping(port, timeout)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(hard_timeout)
    return box["ok"]


def _raw_post(port, path, timeout):
    sock = None
    try:
        body = b"{}"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", port))
        sock.sendall(
            b"POST " + path.encode("ascii") + b" HTTP/1.0\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body)
        response = sock.recv(64)
        return len(response.split()) > 1 and response.split()[1].startswith(b"2")
    except Exception:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def post(port, path, timeout=3.0, hard_timeout=5.0):
    """给服务发一个空 POST（页面的心跳、关闭信号都走 POST）。同样带硬超时。"""
    box = {"ok": False}

    def run():
        box["ok"] = _raw_post(port, path, timeout)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(hard_timeout)
    return box["ok"]


def attach_backend(port, shell_port, hard_timeout=3.0):
    """使用有硬时限的 socket 握手，只有后端确认接管成功才能复用。"""
    box = {"ok": False}
    def run():
        try:
            body = json.dumps({"shport": shell_port}).encode("ascii")
            with socket.create_connection(("127.0.0.1", port), timeout=1.0) as sock:
                sock.sendall(b"POST /api/attach HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                             b"Content-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode("ascii") + b"\r\nConnection: close\r\n\r\n" + body)
                response = b""
                while len(response) < 65536:
                    part = sock.recv(4096)
                    if not part:
                        break
                    response += part
                head, data = response.split(b"\r\n\r\n", 1)
                box["ok"] = head.split()[1] == b"200" and json.loads(data).get("ok") is True
        except Exception:
            pass
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(hard_timeout)
    return box["ok"]


def reusable_backend(port, shell_port, timeout=35.0):
    """接管成功才复用；旧版或已退出的服务等待端口释放，绝不盲目搭车。"""
    deadline = time.monotonic() + timeout
    while ping(port):
        if attach_backend(port, shell_port):
            return port
        if time.monotonic() >= deadline:
            raise RuntimeError("旧后台仍在处理任务或不支持窗口接管，请关闭旧窗口后重试。")
        time.sleep(0.4)
    return None


def read_runtime_port():
    """读 server.py 写下的实际端口。"""
    try:
        with open(RUNTIME_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        port = int(data.get("port") or 0)
        return port or None
    except Exception:
        return None


def start_server(python_exe, config, shell_port=None):
    """
    用 pythonw 拉起 server.py。

    shell_port：外壳控制端口（就是写进页面地址的那个 shport）。传过去是为了让服务端
    能分辨「窗口那个页面」和「别的页面」——只有窗口页面发的关闭信号才算数，
    否则你在浏览器里瞄一眼、或者无界面浏览器抓一下页面，一关就把正在用的程序带走了。

    关键点：
      - 先把旧的 runtime.json 删掉，否则可能读到上一次运行留下的端口；
      - DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP：让服务独立于启动器活着；
      - stdout/stderr 丢掉 —— 用 pythonw 本来也没有控制台。
    """
    try:
        os.remove(RUNTIME_PATH)
    except OSError:
        pass
    creationflags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        creationflags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    args = [python_exe, os.path.join(BASE_DIR, "server.py")]
    if shell_port:
        args += ["--shell-port", str(shell_port)]
    # ⚠️ 服务端的输出**必须**留下来（2026-09-25 改）。
    # 原来这里是 `stdout=DEVNULL, stderr=DEVNULL` —— 后果是**服务端崩溃时的 traceback
    # 凭空消失**：用户在别的电脑上遇到"程序突然没了"，我们手里一条线索都没有，
    # 只能听对方口述。pythonw 本来就没有控制台，丢了就真的没了。
    # 现在接到 `logs/server.log`（追加）：服务端所有 print 和未捕获异常的 traceback 都在里面。
    # 注意：句柄要留给子进程用，所以**不能在父进程关掉**（close_fds=True 时
    # stdout/stderr 传文件对象是允许的，Python 会把它们单独传给子进程）。
    stdout = stderr = None
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR)
        if app_paths.rotation_enabled():
            app_paths.rotate_log(SERVER_LOG_PATH)
        stdout = stderr = open(SERVER_LOG_PATH, "a", encoding="utf-8", errors="replace")
    except Exception:
        # 日志开不了也不能拦住启动 —— 退回原来的行为（丢弃输出）
        stdout = stderr = subprocess.DEVNULL
    subprocess.Popen(
        args,
        cwd=BASE_DIR,
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        close_fds=True,
        env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"),
    )
    # 父进程这份句柄用完了就关，避免占着文件（子进程有自己那一份）
    if stdout not in (None, subprocess.DEVNULL):
        try:
            stdout.close()
        except Exception:
            pass


def wait_for_port(port, timeout=30.0):
    """等程序真正起来：既能读到 runtime.json，也能 ping 通。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        actual = read_runtime_port()
        if actual and ping(actual):
            return actual
        if ping(port):
            return port
        time.sleep(0.4)
    return None


# --------------------------------------------------------------------------- #
# 单实例：第二次双击时把已有窗口提到前面，不再开第二个
# --------------------------------------------------------------------------- #
def acquire_single_instance():
    """
    返回 (是不是第一个实例, 互斥体句柄)。
    互斥体拿不到（极少见）时按「是第一个实例」处理，宁可能开也不要不给开。

    这里有个坑值得写下来：上一个实例关掉窗口之后，还要花一两秒发「页面正在关闭」
    再退出，这段时间互斥体还占着、窗口却已经没了。用户正好在这中间双击的话，
    天真的写法会「找不到窗口 -> 弹框说程序已经在运行」，而屏幕上其实什么都没有。
    所以这里不急着下结论：等一会儿，一边等一边试着把已有窗口提到前面；
    等互斥体释放了就当第一个实例正常启动。

    2026-09-27 补一处：**降级重开那一份不许认领旧窗口**（`--bridge-fallback`）。
    降级的用意是"旧窗口控件坏了，换一个带系统标题栏的"，可父进程 spawn 完就 os._exit，
    子进程有可能在旧窗口还没销毁时先找到它 —— 那样它会"把已有窗口提到前面"然后自己退出，
    用户最后拿到的**还是原来那个坏窗口**（这台电脑实测踩到过：日志里连着出现
    「已把已有窗口提到前面」+「这次不再开新窗口」）。所以这条路上改成：
    等旧的那份真的退掉再开自己的窗口；实在等不到也**硬开一个能用的**
    （两个窗口总好过一个拖不动、关不掉的窗口）。
    """
    replacement = "--bridge-fallback" in sys.argv[1:]
    deadline = time.time() + WAIT_FOR_EXITING_INSTANCE
    try:
        while True:
            handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
            if not handle:
                return True, None
            if ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS:
                return True, handle
            # 已经有实例了：窗口还在的话，提到前面就够了（降级重开时**不走这条**）
            if not replacement and focus_existing_window():
                return False, handle
            if time.time() >= deadline:
                if replacement:
                    log("等了 %s 秒上一份还没退干净 —— 照样开自己的窗口（降级重开）"
                        % WAIT_FOR_EXITING_INSTANCE)
                    return True, handle
                log("等了 %s 秒：既没找到已有窗口，它也没退出" % WAIT_FOR_EXITING_INSTANCE)
                return False, handle
            try:
                ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass
            time.sleep(0.25)
    except Exception:
        return True, None


def nudge_repaint(hwnd):
    """
    强制把整个窗口树失效并立即重画。**专门治"最小化恢复后白屏"**（2026-10-13 新电脑实测）。

    【为什么要它】WebView2 在"最小化 → 恢复"之后可能不重绘：进程活着、后端活着、
    日志里「页面已渲染」也早就打过了，但**屏幕上是一片白**。用户从桌面快捷方式再进来时，
    启动器走的是 `focus_existing_window()`，它只把窗口**提到前面**（日志里那句
    「已把已有窗口提到前面」），并不会让 WebView2 重画 —— 白屏就一直挂着。

    【为什么不用"改尺寸抖一下"】2026-09-24 试过按 1 像素抖动，结果那次改动**递归触发 WM_SIZE**、
    窗口宽度一路涨、UI 卡死。所以这里只发重绘消息，**不碰窗口尺寸、也不碰 WebView2 的内部子窗口**
    （`RedrawWindow` 带 RDW_ALLCHILDREN 就够了，改子窗口尺寸正是历史上"最小化再恢复白屏"的老病根）。
    """
    try:
        if not hwnd:
            return False
        flags = (RDW_INVALIDATE | RDW_ERASE | RDW_FRAME | RDW_ALLCHILDREN | RDW_UPDATENOW)
        ctypes.windll.user32.RedrawWindow(handle(hwnd), None, None, flags)
        log("已强制重绘窗口（治最小化恢复后的白屏）")
        return True
    except Exception:
        log("强制重绘失败：\n%s" % traceback.format_exc())
        return False


def ensure_window_visible(hwnd):
    """
    确认窗口真的在屏幕上；不可见就补一次 SW_SHOW。返回 True 表示"现在它可见"。

    【为什么要它（2026-10-10 查出来的最后一环）】无边框补丁要和 pywebview「把窗口显示出来」
    抢时序：补丁先跑到时，窗口停在"建好了但没显示"的状态——日志里补丁那一行的样式是
    `0x06010000`（**没有** WS_VISIBLE 位 0x10000000），而正常那次是 `0x16010000`。
    后果是最难查的那种：进程活着、后端活着、页面也渲染了（日志里「页面已渲染」照样打），
    但**屏幕上什么都没有**；用户再双击只会等到一句「已把已有窗口提到前面」——
    原始事故里那个"卡住"的手感就是这么来的（当时只知道窗口 `WS_VISIBLE=0`，成因没定）。
    这里补一次显式显示，让"补丁赢不赢时序"都不影响结果。
    """
    try:
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        if user32.IsWindowVisible(hwnd):
            return True
        user32.ShowWindow(hwnd, SW_SHOW)
        visible = bool(user32.IsWindowVisible(hwnd))
        if visible:
            log("窗口建出来时是不可见的（补丁和「显示窗口」抢了时序），已补一次 SW_SHOW")
        return visible
    except Exception:
        log("确认窗口可见性失败（不影响使用）：\n%s" % traceback.format_exc())
        return False


def apply_window_icon(hwnd=None):
    """
    把程序图标（`web/app.ico`）设到窗口上，返回 True 表示设上了。

    【为什么必须显式设（2026-10-10 用户报"图标莫名其妙消失"）】
    窗口的图标**不会**自动跟桌面快捷方式一致：pywebview 建出来的 WinForms 窗体没有图标，
    Windows 就退回宿主进程的图标 —— 也就是 `pythonw.exe` 的 Python 图标。
    结果是：桌面快捷方式是我们的图标，程序跑起来以后**任务栏 / Alt-Tab / 标题栏却是别人的图标**，
    看起来就像"图标消失了"。这里在窗口建好后用 `WM_SETICON` 把大图（ICON_BIG）和
    小图（ICON_SMALL）都设成 `web/app.ico` 里的对应尺寸（那里面 16~256 七种尺寸都装了）。

    句柄优先用调用方给的（无边框那条路已经拿到了）；没给就按窗口标题找 —— 这样
    安全模式 / 带系统标题栏那条路也照样能设上。
    """
    try:
        user32 = ctypes.windll.user32
        if not hwnd:
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            log("设窗口图标：拿不到窗口句柄，跳过（不影响使用）")
            return False
        if not os.path.isfile(APP_ICON):
            log("设窗口图标：找不到 %s（不影响使用）" % APP_ICON)
            return False
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SendMessageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.c_void_p, ctypes.c_void_p]
        ok = False
        # ICON_BIG = 1（任务栏 / Alt-Tab）、ICON_SMALL = 0（标题栏）；LR_LOADFROMFILE=0x10
        for kind in (1, 0):
            handle = user32.LoadImageW(None, APP_ICON, 1, 0, 0, 0x10)
            if handle:
                user32.SendMessageW(hwnd, 0x0080, kind, handle)   # WM_SETICON
                ok = True
        if ok:
            log("已把窗口图标设成 %s" % os.path.basename(APP_ICON))
        else:
            log("设窗口图标失败：LoadImageW 返回空（不影响使用）")
        return ok
    except Exception:
        log("设窗口图标出错（不影响使用）：\n%s" % traceback.format_exc())
        return False


def focus_existing_window():
    """已经有窗口了：把它还原并提到前面。"""
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return False
        if ctypes.windll.user32.IsIconic(hwnd):
            ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
        ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW)  # 隐藏窗口也必须恢复
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return False
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        # ★ 恢复之后一定要**强制重绘**：只"提到前面"的话，WebView2 可能还挂着一片白
        #   （2026-10-13 新电脑实测：最小化 → 双击桌面快捷方式 → 白屏）。
        nudge_repaint(hwnd)
        log("已把已有窗口提到前面（HWND %s）" % hwnd)
        return True
    except Exception:
        log("提窗口失败：\n%s" % traceback.format_exc())
        return False


# --------------------------------------------------------------------------- #
# 窗口位置 / 大小记忆
# --------------------------------------------------------------------------- #
def load_window_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_window_state(state):
    try:
        folder = os.path.dirname(STATE_PATH)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
    except Exception:
        log("窗口状态写不进去：\n%s" % traceback.format_exc())


def position_is_visible(x, y):
    """
    记下来的位置还在不在可见的显示器上？
    拔掉外接屏 / 换显示器之后，旧坐标可能落在屏幕外面，那就别用。

    用 GetSystemMetrics 取「所有显示器拼起来的虚拟桌面」范围来判断。
    刻意不用 MonitorFromPoint：那个 API 要传结构体，而 ctypes 在没有声明
    argtypes 时会把结构体按引用传，容易出一个很隐蔽的错。GetSystemMetrics
    全是整数参数，稳。

    ⚠️ **这个判断只在「进程已经声明 DPI 感知」之后才准**（2026-09-27 加）：
    不感知的进程拿到的是虚拟化坐标，缩放 150% 的屏上跟真实坐标差 1.5 倍。
    现在 main() 第一件事就声明感知（见 winenv.enable_dpi_awareness），所以是物理像素。
    """
    try:
        user32 = ctypes.windll.user32
        left = user32.GetSystemMetrics(winenv.SM_XVIRTUALSCREEN)
        top = user32.GetSystemMetrics(winenv.SM_YVIRTUALSCREEN)
        width = user32.GetSystemMetrics(winenv.SM_CXVIRTUALSCREEN)
        height = user32.GetSystemMetrics(winenv.SM_CYVIRTUALSCREEN)
        if width <= 0 or height <= 0:
            return True
        x, y = int(x), int(y)
        # 窗口左上角要落在虚拟桌面里，而且不能只露出一个角
        return (left <= x < left + width - 200) and (top <= y < top + height - 100)
    except Exception:
        return True


def read_splash_html():
    """
    把启动画面读成字符串。

    故意用 html= 直接塞给窗口，而不是让窗口去加载 web/splash.html 这个本地文件：
    本地文件路径在各个 pywebview 版本里写法（裸路径 / file:// 开头）不完全一致，
    塞字符串就没这个不确定性。启动画面里没有任何外部资源，所以塞字符串没副作用。
    """
    try:
        with open(SPLASH_PATH, "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        log("读启动画面失败（改成直接开页面）：\n%s" % traceback.format_exc())
        return None


# --------------------------------------------------------------------------- #
# 无边框窗口：自己补回系统原本免费提供的能力
# --------------------------------------------------------------------------- #
# 去掉系统标题栏会**一起丢掉**：拖动窗口、鼠标拉边框改大小、圆角、投影阴影。
# 下面这套补法是照实测结果来的（见 work/probe_borderless.py 的验证输出）：
#   - 给窗口加 WS_THICKFRAME -> Windows 就把「鼠标拉边框改大小」和系统投影还给窗口
#   - DWM 属性 33 = 圆角
#   - 拖动**不能用 pywebview 自带的 easy_drag**：实测它有「松开鼠标后窗口还黏着光标」
#     的毛病。
#   - 拖动也**不能用 ReleaseCapture + SendMessage(WM_NCLBUTTONDOWN, HTCAPTION)**：
#     实测这一条会把窗口卡死。原因：那个消息会让 Windows 进入一个「等鼠标松开」的模态
#     循环，而我们的调用不是从 GUI 线程发起的，松开事件进不了那个循环，它就永远等下去。
#     所以拖动改成 begin_move() + move_by()：页面记鼠标位移，这边用 pywebview 的
#     window.move() 跟着搬。少一点原生顺滑，但确定能用、不会卡死。
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
GWL_STYLE = -16
GWLP_WNDPROC = -4
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

# ---------------------------------------------------------------------------
# 关于那圈「黑框」（2026-09-22 用像素测量查清的）
#
# 给窗口补 WS_THICKFRAME 会顺便带出来一条 7 像素宽的**非客户区**（鼠标拖边缘改大小
# 就靠它）。实测这 7 像素不是纯色：顶部偏暗蓝灰、左侧偏蓝 —— 它是 Windows 把这
# 区域渲染成**透出后面的桌面（带模糊）**，壁纸偏暗时看着就是一条黑边。
# 而且只有最外面 1 像素受 DWM 的边框颜色控制，里面 6 像素改不了 —— 所以
# 「把边框颜色调成和页面一样」这条路走不通。
#
# 唯一的真修法：让窗口不留非客户区（客户区 = 整个窗口），内容铺满到边缘。
# 做法是接管窗口消息，对 WM_NCCALCSIZE 直接返回 0。
# 连带代价：鼠标拖边缘改大小没了（WebView 盖住整个窗口，Windows 接不到边缘），
# 所以拖边改大小改成页面自己实现（见 ShellApi.begin_resize / resize_by）。
WM_NCCALCSIZE = 0x0083
MONITOR_DEFAULTTONEAREST = 2
SWP_NOSIZE_ = 0x0001
SWP_NOMOVE_ = 0x0002
SWP_NOZORDER_ = 0x0004
SWP_NOACTIVATE_ = 0x0010
SWP_FRAMECHANGED_ = 0x0020


def handle(hwnd):
    """
    把整数句柄包成 ctypes 的指针再传给 Win32。
    不包的话 ctypes 会把 Python 整数按 32 位 int 传，64 位下句柄值一大就传错了。
    """
    return ctypes.c_void_p(hwnd)


_WIN32_READY = {"done": False}


def pick_window_long_procs(user32):
    """
    挑出「读写窗口样式 / 窗口过程」的那一对函数，返回 `(getter, setter)`。

    ⚠️ 为什么不能直接写 `user32.GetWindowLongPtrW`（2026-09-27，试用的人那台机器）：
    **32 位 Windows 的 user32 里没有 `*PtrW` 这两个导出** —— 它们只在 64 位存在；
    32 位要用 `GetWindowLongW` / `SetWindowLongW`（头文件里那个 `Ptr` 名字就是个宏，
    在 32 位下指向 W 版）。那台机器的 Python 是 `Python312-32`（32 位），于是
    `setup_win32()` 每次都抛 `AttributeError: function 'GetWindowLongPtrW' not found`：
    无边框补丁失败 **33 次**、拖动失败 **124 次**、最小化/最大化全失效 ——
    用户看到的就是"窗口拖不动、按钮点不动"。这台电脑 64 位，永远复现不了。

    纯函数（只 getattr），能单独测。
    """
    getter = getattr(user32, "GetWindowLongPtrW", None) or getattr(user32, "GetWindowLongW", None)
    setter = getattr(user32, "SetWindowLongPtrW", None) or getattr(user32, "SetWindowLongW", None)
    return getter, setter


def setup_win32():
    """
    一次性把要用到的 Win32 函数的参数/返回类型声明好。
    不声明的话，GetWindowLongPtrW 这类返回 LONG_PTR 的函数会被按 32 位返回，
    样式值可能被截断 —— 这种错在 64 位系统上很隐蔽。

    `*PtrW` 在 32 位 Python 上不存在，所以走 `pick_window_long_procs` 挑（见那个函数的说明），
    选好的两个函数放进 `_WIN_LONG`，后面所有读写都用它们 —— **不要再直接写 `*PtrW`**。
    """
    if _WIN32_READY["done"]:
        return
    user32 = ctypes.windll.user32
    getter, setter = pick_window_long_procs(user32)
    if getter is None or setter is None:
        # 理论上到不了这儿（两种名字至少有一种在）；真遇到就明确报出来，别让调用方一脸懵
        raise OSError("user32 里既没有 *PtrW 也没有 W 版的 Get/SetWindowLong")
    _WIN_LONG["get"], _WIN_LONG["set"] = getter, setter
    getter.restype = ctypes.c_ssize_t
    getter.argtypes = [ctypes.c_void_p, ctypes.c_int]
    setter.restype = ctypes.c_ssize_t
    setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
    user32.IsZoomed.argtypes = [ctypes.c_void_p]
    user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_size_t, ctypes.c_ssize_t]
    user32.ReleaseCapture.argtypes = []
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_uint]
    _WIN32_READY["done"] = True


def apply_borderless_style(window):
    """把无边框窗口缺的那几样补回来。成功返回 HWND，失败返回 None。"""
    hwnd = get_hwnd(window)
    if not hwnd:
        return None
    try:
        setup_win32()
        user32 = ctypes.windll.user32
        style = _WIN_LONG["get"](handle(hwnd), GWL_STYLE)
        want = style | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU
        _WIN_LONG["set"](handle(hwnd), GWL_STYLE, want)
        # SWP_FRAMECHANGED 让 Windows 按新样式重画边框；另外三个是「别动位置/大小/层级」
        user32.SetWindowPos(handle(hwnd), None, 0, 0, 0, 0,
                            0x0020 | 0x0001 | 0x0002 | 0x0004)
        corner = ctypes.c_int(DWMWCP_ROUND)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            handle(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(corner), ctypes.sizeof(corner))
        after = _WIN_LONG["get"](handle(hwnd), GWL_STYLE)
        log("无边框补丁：样式 0x%08X -> 0x%08X  WS_THICKFRAME=%s  DWM圆角 hr=%d"
            % (style & 0xFFFFFFFF, after & 0xFFFFFFFF,
               "有" if (after & WS_THICKFRAME) else "没有（被改回去了）", hr))
        return hwnd
    except Exception:
        log("无边框补丁失败：\n%s" % traceback.format_exc())
        return None


def apply_borderless_when_ready(window, timeout=8.0):
    """窗口句柄要等 GUI 真正跑起来才有，所以这里等一小会儿再补样式。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = apply_borderless_style(window)
        if hwnd:
            return hwnd
        time.sleep(0.25)
    # 这句话以前写成「等不到窗口句柄」，但拿不到句柄只是失败的一种原因
    #（2026-09-27 那台 32 位机器是样式接口不存在）—— 具体原因看上面那条失败记录。
    log("8 秒内没能把无边框补丁打上（原因见上面的失败记录）")
    return None


# --------------------------------------------------------------------------- #
# 接管窗口消息：把「非客户区」去掉，让内容铺满整个窗口（就是那圈黑框的根治办法）
# --------------------------------------------------------------------------- #
class _NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wt.RECT * 3), ("lppos", ctypes.c_void_p)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, ctypes.c_uint,
                              ctypes.c_size_t, ctypes.c_ssize_t)
_OLD_WNDPROC = {}      # hwnd -> 原来的窗口过程
_WNDPROC_REFS = []     # 必须一直留着引用，被回收掉窗口就崩了


def _wnd_proc(hwnd, msg, wparam, lparam):
    """
    我们自己接管的窗口过程。只处理一条消息，其它一律转回原来的处理函数。

    WM_NCCALCSIZE：Windows 问「这个窗口的非客户区有多宽」。平时（wParam 为真）我们
    直接返回 0，意思是「没有非客户区，客户区就是整个窗口」—— 那圈黑边就没有了，
    页面铺满整个窗口，标题栏也就真正顶到窗口最上沿。
    """
    try:
        if msg == WM_NCCALCSIZE and wparam:
            user32 = ctypes.windll.user32
            if user32.IsZoomed(hwnd):
                # 最大化时 Windows 给的窗口矩形比工作区大出「边框那几像素」
                #（实测是 -7,-7 到 1927,1087）。如果直接把它当客户区，
                # 内容会跑到屏幕外各多出 7 像素，所以这里改成按工作区取值。
                params = ctypes.cast(lparam, ctypes.POINTER(_NCCALCSIZE_PARAMS)).contents
                monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
                info = _MONITORINFO()
                info.cbSize = ctypes.sizeof(_MONITORINFO)
                if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    params.rgrc[0] = info.rcWork
            return 0
    except Exception:
        # 这里出错绝不能往外抛：窗口过程里抛异常会让窗口直接坏掉
        pass
    old = _OLD_WNDPROC.get(hwnd)
    if not old:
        return 0
    try:
        return ctypes.windll.user32.CallWindowProcW(
            ctypes.c_void_p(old), hwnd, msg, wparam, lparam)
    except Exception:
        return 0


def hook_window_proc(hwnd):
    """
    把窗口过程换成我们自己的。成功返回 True。
    失败也没关系：那就保持原样（那圈黑边还在，但程序照常用）。
    """
    if not hwnd:
        return False
    try:
        setup_win32()
        user32 = ctypes.windll.user32
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_uint, ctypes.c_size_t,
                                           ctypes.c_ssize_t]
        # ⚠️ 用 setup_win32() 挑好的那一对（32 位 Python 上没有 *PtrW，见 pick_window_long_procs）
        _WIN_LONG["set"].argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        proc = _WNDPROC(_wnd_proc)
        old = _WIN_LONG["set"](ctypes.c_void_p(hwnd), GWLP_WNDPROC,
                               ctypes.cast(proc, ctypes.c_void_p))
        if not old:
            log("接管窗口消息失败：设置窗口过程没返回原来的处理函数")
            return False
        _WNDPROC_REFS.append(proc)
        _OLD_WNDPROC[hwnd] = old
        # 让 Windows 按新的规则重算一次非客户区
        user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
                            SWP_NOMOVE_ | SWP_NOSIZE_ | SWP_NOZORDER_ |
                            SWP_NOACTIVATE_ | SWP_FRAMECHANGED_)
        log("已接管窗口消息：客户区改为铺满整个窗口（那圈黑边应该没了）")
        return True
    except Exception:
        log("接管窗口消息时出错（保持原样）：\n%s" % traceback.format_exc())
        return False


class ShellApi(object):
    """
    暴露给页面的窗口控制（页面里写作 window.pywebview.api）。
    只有外壳加载的页面才有这个对象；用普通浏览器打开时它是 undefined，
    页面会自己退化成「没有自绘标题栏」的样子。
    """

    def __init__(self):
        self.window = None
        self._move_origin = None
        self._resize_origin = None
        self._resize_edge = ""
        self._move_error_logged = False
        self._logged = set()

    def _log_once(self, key, message):
        """同一个 key 只记一次，免得拖动时把日志刷爆。"""
        if key in self._logged:
            return
        self._logged.add(key)
        log(message)

    def shell_hello(self):
        """
        页面确认「JS -> Python 这条通路是通的」之后会调这个。
        日志里出现这一行，说明自绘标题栏的按钮和拖动都有桥可用；
        没出现，说明桥没连上 —— 那按钮点不动、也拖不动，方向就清楚了。
        """
        self._log_once("hello", "页面已连上外壳桥（JS -> Python 通路正常）")
        SHELL_ALIVE.set()
        return True

    def shell_rendered(self):
        """
        页面**第一次真的渲染出来**之后会调这个（applyState 里 render() 之后发一次）。

        【为什么要它】2026-09-23 出过一次「整窗白屏」：服务、日志、接口全都正常，
        页面连 hello 都发了，但 WebView2 就是没把内容画出来（截下来 99.5% 是窗口底色）。
        hello 只能证明「脚本跑起来了」，证明不了「画出来了」——所以要这条更晚的信号，
        让启动器在等不到的时候自己重载一次（见 watch_first_render）。
        """
        if not PAGE_RENDERED.is_set():
            PAGE_RENDERED.set()
            log("页面已渲染（首屏真的画出来了）")
        return True

    def _hwnd(self):
        try:
            return get_hwnd(self.window) if self.window else None
        except Exception:
            return None

    def begin_move(self):
        """
        页面上的拖动条被按下时调用：记住窗口现在的位置。
        后面每次鼠标动，页面报一个相对按下点的位移，这边按位移搬窗口。

        刻意不用 ReleaseCapture + WM_NCLBUTTONDOWN 那条原生路子 —— 实测它会把窗口
        卡死（详见文件顶部那段说明）。
        """
        hwnd = self._hwnd()
        if not hwnd:
            self._log_once("begin_no_hwnd", "begin_move：拿不到窗口句柄")
            return False
        try:
            setup_win32()
            rect = wt.RECT()
            ctypes.windll.user32.GetWindowRect(handle(hwnd), ctypes.byref(rect))
            self._move_origin = (rect.left, rect.top)
            self._log_once("begin_ok", "拖动开始（起点 %s,%s）" % (rect.left, rect.top))
            return True
        except Exception:
            log("begin_move 失败：\n%s" % traceback.format_exc())
            return False

    def move_by(self, dx, dy):
        """把窗口搬到「按下时的位置 + 位移」。拖动过程中会被高频调用。"""
        if not self.window or not self._move_origin:
            return False
        try:
            setup_win32()
            hwnd = self._hwnd()
            if not hwnd:
                return False
            x = int(self._move_origin[0]) + int(dx)
            y = int(self._move_origin[1]) + int(dy)
            # 直接用 Win32 搬窗口，不走 pywebview 的 window.move()：
            # SetWindowPos 从别的线程调用是安全的（它只是投递一个位置变更消息），
            # 也不用猜 pywebview 内部是怎么切线程的。
            # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            ctypes.windll.user32.SetWindowPos(handle(hwnd), None, x, y, 0, 0,
                                              0x0001 | 0x0004 | 0x0010)
            self._log_once("move_ok", "第一次按位移搬窗口成功：%s,%s" % (x, y))
            return True
        except Exception:
            # 拖动时这个函数每秒会被调几十次，出错只记一次，别把日志刷爆
            if not self._move_error_logged:
                self._move_error_logged = True
                log("move_by 失败：\n%s" % traceback.format_exc())
            return False

    def begin_resize(self, edge):
        """
        页面上的边缘被按住时调用：记住窗口现在的矩形和拖的是哪条边。

        为什么要自己做：去掉非客户区之后（就是修黑框那一步），WebView 盖住了整个窗口，
        Windows 就接不到「鼠标在窗口边缘」这件事了，原生拖边改大小随之失效。
        所以改成页面负责收鼠标位移，这边照着算新尺寸。
        """
        hwnd = self._hwnd()
        if not hwnd:
            return False
        try:
            setup_win32()
            rect = wt.RECT()
            ctypes.windll.user32.GetWindowRect(handle(hwnd), ctypes.byref(rect))
            self._resize_origin = (rect.left, rect.top,
                                   rect.right - rect.left, rect.bottom - rect.top)
            self._resize_edge = str(edge or "")
            self._log_once("resize_begin",
                           "开始拖边缘改大小（%s）" % self._resize_edge)
            return True
        except Exception:
            log("begin_resize 失败：\n%s" % traceback.format_exc())
            return False

    def resize_by(self, dx, dy):
        """按位移改窗口大小。拖边缘时被高频调用。"""
        if not self.window or not self._resize_origin:
            return False
        try:
            setup_win32()
            hwnd = self._hwnd()
            if not hwnd:
                return False
            x, y, w, h = self._resize_origin
            dx, dy = int(dx), int(dy)
            edge = self._resize_edge
            if "l" in edge:
                new_x, new_w = x + dx, w - dx
            elif "r" in edge:
                new_x, new_w = x, w + dx
            else:
                new_x, new_w = x, w
            if "t" in edge:
                new_y, new_h = y + dy, h - dy
            elif "b" in edge:
                new_y, new_h = y, h + dy
            else:
                new_y, new_h = y, h
            # 别缩到比最小尺寸还小；从左/上边缩的时候还要把位置一起顶住，
            # 否则窗口会「跑偏」
            if new_w < MIN_WINDOW_WIDTH:
                if "l" in edge:
                    new_x = x + w - MIN_WINDOW_WIDTH
                new_w = MIN_WINDOW_WIDTH
            if new_h < MIN_WINDOW_HEIGHT:
                if "t" in edge:
                    new_y = y + h - MIN_WINDOW_HEIGHT
                new_h = MIN_WINDOW_HEIGHT
            ctypes.windll.user32.SetWindowPos(handle(hwnd), None, new_x, new_y,
                                              new_w, new_h,
                                              SWP_NOZORDER_ | SWP_NOACTIVATE_)
            return True
        except Exception:
            if not self._move_error_logged:
                self._move_error_logged = True
                log("resize_by 失败：\n%s" % traceback.format_exc())
            return False

    def minimize(self):
        self._log_once("minimize", "收到最小化请求")
        hwnd = self._hwnd()
        if hwnd:
            try:
                setup_win32()
                ctypes.windll.user32.ShowWindow(handle(hwnd), SW_MINIMIZE)
            except Exception:
                log("最小化失败：\n%s" % traceback.format_exc())
        return True

    def repaint(self):
        """页面在"最小化恢复、重新可见"时叫一次：强制重绘整窗（治白屏）。

        与 `focus_existing_window()` 里那次是两条路：那条是"从桌面快捷方式再进来"，
        这条是"用户从任务栏把窗口恢复回来"——后者不经过启动器的任何代码，所以必须由页面发起。
        """
        hwnd = self._hwnd()
        if not hwnd:
            return False
        try:
            setup_win32()
            return nudge_repaint(handle(hwnd))
        except Exception:
            log("重绘请求失败：\n%s" % traceback.format_exc())
            return False

    def toggle_maximize(self):
        self._log_once("maximize", "收到最大化/还原请求")
        hwnd = self._hwnd()
        if not hwnd:
            return False
        try:
            setup_win32()
            user32 = ctypes.windll.user32
            if user32.IsZoomed(handle(hwnd)):
                user32.ShowWindow(handle(hwnd), SW_RESTORE)
                nudge_repaint(hwnd)      # 还原后强制重绘（同上：治白屏）
            else:
                user32.ShowWindow(handle(hwnd), SW_MAXIMIZE)
            return True
        except Exception:
            log("最大化切换失败：\n%s" % traceback.format_exc())
            return False

    def is_maximized(self):
        hwnd = self._hwnd()
        if not hwnd:
            return False
        try:
            setup_win32()
            return bool(ctypes.windll.user32.IsZoomed(handle(hwnd)))
        except Exception:
            return False

    def close_window(self):
        self._log_once("close", "收到关闭窗口请求")
        try:
            if self.window:
                self.window.destroy()
        except Exception:
            log("关窗口失败：\n%s" % traceback.format_exc())
        return True

    def open_external(self, url):
        """
        把外部链接交给系统默认浏览器打开。

        为什么不靠 pywebview 的 OPEN_EXTERNAL_LINKS_IN_BROWSER 设置：
        那个只是 pywebview 的一个开关，实际行为要看它内部实现（而且这个版本
        JS 桥本身就没接上，不能把宝押在它的设置上）。这里用 os.startfile，
        是 Windows 官方的「用默认程序打开」，行为确定。
        """
        url = str(url or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return False
        # 我们自己的本地服务不开到浏览器里去
        if "127.0.0.1" in url or "localhost" in url:
            return False
        try:
            os.startfile(url)
            self._log_once("open_ext", "已把外部链接交给系统浏览器打开")
            return True
        except Exception:
            log("打开外部链接失败：\n%s" % traceback.format_exc())
            return False

    def handle(self, action, payload):
        """
        把页面上报的动作分派到具体方法，返回 True/False。

        ⚠️ 这里的动作名必须和 index.html 里 shSend("...") 用的字符串**逐字一致**。
        曾经漏了一个：页面发 close_window、这里只认 close，结果点 X 毫无反应，
        而且只在日志里表现为一句「收到不认识的窗口命令」。所以下面顺手兼容了几种叫法。
        """
        if action == "hello":
            return self.shell_hello()
        if action == "rendered":
            return self.shell_rendered()
        if action == "begin_move":
            return self.begin_move()
        if action == "move_by":
            return self.move_by(payload.get("dx", 0), payload.get("dy", 0))
        if action == "begin_resize":
            return self.begin_resize(payload.get("edge"))
        if action == "resize_by":
            return self.resize_by(payload.get("dx", 0), payload.get("dy", 0))
        if action == "minimize":
            return self.minimize()
        if action == "repaint":
            return self.repaint()
        if action == "toggle_maximize":
            return self.toggle_maximize()
        if action in ("close", "close_window", "quit", "exit"):
            return self.close_window()
        if action == "open_external":
            return self.open_external(payload.get("url"))
        log("收到不认识的窗口命令：%s" % action)
        return False


# 首屏渲染看门狗用的事件：页面渲染成功后回一个 rendered（见 ShellApi.shell_rendered）。
# 放在模块级，是因为「发信号」在 ShellApi 里、「等信号」在窗口线程里，两边拿同一个事件最省事。
PAGE_RENDERED = threading.Event()

# 「外壳控制通道通了没有」用的事件：页面能发出窗口命令时回一个 hello
# （见 ShellApi.shell_hello）。和 PAGE_RENDERED 一样是模块级的 ——
# 发信号的（ShellApi）和等信号的（窗口线程）拿同一个事件最省事。
#
# 【为什么要它，2026-09-25 加】有台双屏的机器反馈：安全模式能用，但正常启动
# 「拖不动窗口、也不能最小化/最大化」。看代码就能确定原因范围 ——
# 那三件事**全都靠这条 HTTP 通道**（页面 fetch 到启动器的控制端口）。
# 通道不通 = 点了一点反应都没有（fetch 是发完不管的，连报错都不会有），
# 用户会卡在一个既不能拖也不能关的窗口里。
# 这台电脑复现不了（没有双屏），所以这里不改猜测性的代码，改成**兜底**：
# 通道没通就自动降级成「安全模式那种带系统标题栏的窗口」，拖动/最小化/最大化
# 交给 Windows 自己管 —— 他一定能用。
SHELL_ALIVE = threading.Event()


def watch_first_render(win, url, delay=8.0):
    """
    切到真实页面之后，盯着「首屏到底画出来没有」。

    页面渲染成功会回一个 rendered（见 index.html 的 applyState）；
    等不到就**自动重载一次**，再等不到就写日志 —— 总比让用户对着白窗口自己猜强。
    2026-09-23 真的白屏过一次（服务全正常，就是没画出来），重开程序才好；
    有了这条，程序自己就能恢复。

    只在启动阶段跑这一次：正常情况下面 1 秒内就 set 了，线程立刻退出。
    """
    def worker():
        if PAGE_RENDERED.wait(delay):
            return
        log("⚠️ 等了 %.0f 秒还没收到「页面已渲染」，自动重载一次" % delay)
        try:
            sep = "&" if "?" in url else "?"
            win.load_url("%s%sr=%d" % (url, sep, int(time.time())))
        except Exception:
            log("自动重载失败：\n%s" % traceback.format_exc())
            return
        if PAGE_RENDERED.wait(delay):
            log("重载之后页面渲染正常")
        else:
            log("⚠️ 重载后页面仍未渲染出来（窗口可能还是空白）：可在页面里刷新，或者重开程序")

    threading.Thread(target=worker, daemon=True).start()


def watch_shell_bridge(delay=20.0):
    """
    盯着「外壳控制通道到底通了没有」；没通就**自动换成安全模式那种窗口重开一份**。

    【要解决的问题】窗户的拖动、最小化、最大化**全靠页面 → 启动器那条 HTTP 通道**
    （页面 fetch 到控制端口）。通道不通时，表现是「点了一点反应都没有」——
    因为 `fetch` 是发完不管的（`mode: no-cors` + `.catch(function(){})`），
    连报错都不会留下。用户就卡在一个既拖不动也关不掉的窗口里。

    2026-09-25 有一台双屏机器反馈的正是这个（安全模式正常，直接启动不行）。
    这台电脑复现不了，所以不做猜测性的修改，改成兜底：
    **宁可用「带系统标题栏的窗口」（丑一点但一定能拖能关），也不要给一个动不了的窗口。**

    判断依据用 `SHELL_ALIVE`（页面发过 `hello`）。为什么不是"控制端口在监听"就够了：
    端口开着只能证明**启动器这边**起来了，证明不了**页面发得出去**——
    而失败恰恰可能出在页面那一侧（代理、安全软件拦 127.0.0.1 的 fetch 等）。

    只在 `borderless` 模式下有意义（带系统标题栏时窗口本来就能拖）。
    """
    def worker():
        if SHELL_ALIVE.wait(delay):
            return
        log("⚠️ 等了 %.0f 秒都没收到外壳桥的 hello —— 窗口控件（拖动/最小化/最大化）"
            "在这个环境里不可用" % delay)
        log("   自动改用「带系统标题栏」的方式重开一份窗口（等价于安全模式），"
            "让拖动和窗口按钮交回给 Windows 管")
        relaunch_safe_mode("外壳控制通道不通（页面发不出窗口命令）")

    threading.Thread(target=worker, daemon=True).start()


def relaunch_safe_mode(reason):
    """
    用 `--safe-mode` 重开一份自己，然后退出当前这份。

    为什么复用 `--safe-mode`：那条路已经在同一台机器上被验证过能用
    （用户反馈「安全模式正常使用」），所以降级走它最稳，不另造一套。
    加 `--bridge-fallback` 标记：万一降级之后**还是**不通，别再无限重开下去。
    """
    if "--bridge-fallback" in sys.argv[1:]:
        log("已经降级过一次了，不再重开（避免无限循环）")
        alert("窗口控件在这个环境里用不了，而且降级之后仍然不行。\n\n"
              "请把这个日志文件发给开发者：\n%s" % LOG_PATH)
        return False
    here = os.path.dirname(os.path.abspath(__file__))
    args = [sys.executable, os.path.abspath(__file__)] + sys.argv[1:] + [
        "--safe-mode", "--bridge-fallback"]
    log("原因：%s" % reason)
    try:
        subprocess.Popen(
            args, cwd=here, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    except OSError as exc:
        log("重开失败（%s）—— 就地继续，窗口可能拖不动" % exc)
        return False
    log(">>> 已用安全模式重开一份窗口，当前这份退出")
    # 让当前这份彻底退掉：控制端口/主循环都归 0，否则单实例互斥体会挡住新那份
    try:
        os._exit(0)
    except Exception:
        pass
    return True


def start_control_server():
    """
    开一个只监听这台电脑的极小 HTTP 服务，专门收页面发来的窗口控制命令。
    成功返回 (端口号, ShellApi 实例)，失败返回 (None, None)。

    为什么不用 pywebview 自带的 js_api 桥：
      实测在「先显示启动画面、再 load_url 切到真实页面」这个流程下，切过去之后
      页面里根本找不到 window.pywebview（日志里连「页面已连上外壳桥」都没有），
      于是自绘标题栏的按钮和拖动全部静默失效。这个桥在这个流程里不可靠。

    HTTP 这条路是页面本来就在用的方式（它一直在跟 127.0.0.1 的服务说话），
    不依赖任何注入机制。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    api = ShellApi()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            action = str(payload.get("action") or "")
            try:
                ok = bool(api.handle(action, payload))
            except Exception:
                log("处理窗口命令失败：\n%s" % traceback.format_exc())
                ok = False
            body = json.dumps({"ok": ok}).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass

        def do_OPTIONS(self):
            # 页面用的是 no-cors 的简单请求，其实用不到这个，留着以防万一
            try:
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                pass

        def log_message(self, *args):
            pass  # 别往控制台写日志

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except Exception:
        log("窗口控制服务起不来：\n%s" % traceback.format_exc())
        return None, None
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("窗口控制服务已就绪：127.0.0.1:%d" % port)
    return port, api


def startup_geometry():
    """
    算出这次开窗口该用的位置和大小。**全部按物理像素**（前提：已声明 DPI 感知，见 main()）。

    2026-09-27 加的两件事（双屏 + 缩放那台机器上报的"窗口被拉长、溢出到副屏"）：
      · 算出矩形后**夹进目标显示器的可用区域**——上次记的尺寸可能来自另一块更大的屏，
        或者来自另一次缩放，直接用就会得到"比屏幕还大"的窗口；
      · 把这台显示器的**缩放倍数**记到 GEOMETRY 里，建窗口时要用它把物理像素换算成
        pywebview 要的逻辑像素（它自己会再乘一次缩放，见 winenv.py 顶部那段说明）。
    """
    state = load_window_state()
    width = int(state.get("w") or 0)
    height = int(state.get("h") or 0)
    if width < MIN_WINDOW_WIDTH or height < MIN_WINDOW_HEIGHT:
        # 记下来的尺寸不合理（比如窗口最小化时量到的 160x28），当「没有记录」处理，
        # 用默认大小 —— 而不是把它夹到最小尺寸。夹出来的 900x640 又小又难看，
        # 2026-09-22 就是这么发生的。
        if width or height:
            log("上次记的窗口尺寸 %sx%s 不合理，改用默认 %sx%s"
                % (width, height, DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT))
        width = DEFAULT_WINDOW_WIDTH
        height = DEFAULT_WINDOW_HEIGHT
    x = state.get("x")
    y = state.get("y")
    if x is not None and y is not None and position_is_visible(x, y):
        log("沿用上次的窗口位置 %s,%s 大小 %sx%s" % (x, y, width, height))
    else:
        if x is not None:
            log("上次的窗口位置 %s,%s 已经不在可见显示器上，忽略" % (x, y))
        x, y = None, None

    work, scale = winenv.monitor_work_area(x, y)
    GEOMETRY["scale"] = scale
    GEOMETRY["work"] = work
    if scale != 1.0:
        log("这台显示器缩放 %.0f%%：可用区域 %d,%d %dx%d（物理像素）"
            % (scale * 100, work[0], work[1], work[2] - work[0], work[3] - work[1]))
    if x is None or y is None:
        # 没有可用的历史位置：**位置仍然交回 pywebview 居中**（老行为不变），
        # 但尺寸照样夹一次（按这台显示器的可用区域）—— 这是 2026-09-27 防"比屏幕还大"那条。
        _cx, _cy, fw, fh = winenv.fit_to_work_area(None, None, width, height, work,
                                                   MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        fx, fy = None, None
    else:
        fx, fy, fw, fh = winenv.fit_to_work_area(x, y, width, height, work,
                                                 MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
    if (fw, fh) != (width, height) or (fx, fy) != (x, y):
        log("窗口矩形夹进可用区域：%sx%s @%s,%s -> %sx%s @%s,%s"
            % (width, height, x, y, fw, fh, fx, fy))
    return fw, fh, fx, fy, bool(state.get("maximized"))


# 窗口句柄只取一次就记下来。
# 为什么必须缓存：window.native 是跨线程去问 GUI 线程要对象，反复访问有触发死锁的风险
#（实测出现过一次「启动到一半整个卡住」）。句柄拿到手之后就是个整数，
# 之后所有 Win32 调用都用它，不再碰 window.native。
_HWND = {"value": None}

# setup_win32() 挑好的那一对「读写窗口样式/窗口过程」的函数（64 位是 *PtrW，32 位是 W 版）。
# 所有读写都走这里，别再直接写 *PtrW —— 32 位 Python 上没有那个导出（见 pick_window_long_procs）。
_WIN_LONG = {"get": None, "set": None}

# 这次建窗口用的几何事实（startup_geometry 填）：缩放倍数 + 目标显示器的可用区域。
# 为什么要跨函数传：建窗口时要拿 scale 把物理像素换算成 pywebview 要的逻辑像素，
# 建完还要拿物理像素钉一次（见 winenv.py 顶部那段 DPI 说明）。
GEOMETRY = {"scale": 1.0, "work": None}


def resolve_pin_position(x, y, current_left, current_top):
    """
    「钉窗口」时要用的位置：`x`/`y` 是 None 就沿用窗口**当前**的位置。

    ⚠️ 为什么必须处理 None（2026-09-27，被升级门当场抓到）：**全新安装没有
    `data/window_state.json`**，`startup_geometry()` 按契约把位置返回成 None
    （让 pywebview 自己居中）。第一版 `apply_window_geometry` 直接 `int(x)`，
    于是**所有新用户第一次启动就崩在启动路径上**（`TypeError: int() argument ...
    not 'NoneType'`），窗口停在启动画面、服务起不来 —— 而开发者这台电脑因为早就有了
    `window_state.json`，永远碰不到。纯函数，能单独测。
    """
    return (current_left if x is None else int(x),
            current_top if y is None else int(y))


def apply_window_geometry(hwnd, x, y, width, height):
    """
    按**物理像素**把窗口钉到指定矩形，返回实际落到的 `(x, y, w, h)`（失败返回 None）。

    `x`/`y` 允许是 None：那时只钉尺寸、位置沿用当前（见 resolve_pin_position）。

    为什么建完窗口还要自己钉一次：pywebview 把 create_window() 的宽高/坐标当逻辑像素、
    建窗口时会再乘一次那台显示器的缩放（`winforms.py:198-220`）。缩放 150% 的屏上，
    我们「物理上要 1400x900」会被它做成 2100x1350 —— 比屏幕还大、溢出到副屏。
    钉一次就与 pywebview 怎么算无关了。
    """
    try:
        setup_win32()
        user32 = ctypes.windll.user32
        rect = wt.RECT()
        if x is None or y is None:
            user32.GetWindowRect(handle(hwnd), ctypes.byref(rect))
        x, y = resolve_pin_position(x, y, rect.left, rect.top)
        user32.SetWindowPos(handle(hwnd), None, x, y, int(width), int(height),
                            SWP_NOZORDER_ | SWP_NOACTIVATE_)
        user32.GetWindowRect(handle(hwnd), ctypes.byref(rect))
        return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
    except Exception:
        log("按物理像素摆窗口失败（不影响使用）：\n%s" % traceback.format_exc())
        return None


def describe_pin_target(x, y, width, height, landed):
    """
    「钉窗口：目标 … 实际 …」那一行日志。

    ⚠️ 单独抽出来是因为这里踩过一个大坑（2026-09-27，升级门当场抓到）：
    原来这行写成 `"目标 %d,%d …" % (x, y, …)`，而**全新安装**时 x/y 是 None
    （没有 `window_state.json`，位置交给 pywebview 居中）—— `%d` 遇到 None 抛 TypeError，
    这句又**不在任何 try 里**，于是 worker 线程直接死掉：窗口停在启动画面、服务起不来，
    60 秒后看门狗报「卡在打无边框补丁」。为此现在**一律用 %s**（None 打出来就是 None），
    并且 worker 里那几段也各自包了 try。纯函数，能单独测。
    """
    return ("钉窗口：目标 %s,%s %sx%s（物理像素，None = 沿用当前位置）；实际 %s"
            % (x, y, width, height, landed))


def pin_window_geometry(window, x, y, width, height, timeout=6.0):
    """
    等窗口句柄就绪 -> 按物理像素钉到 (x,y,width,height) -> 记一行「目标 vs 实际」。

    只在「不用 pywebview 自己最大化」的路径上调（最大化和钉位置会互相顶掉）。
    **绝不抛异常**：钉窗口是锦上添花，任何失败都只写一行日志、让启动继续（踩过上面的坑）。
    """
    try:
        hwnd = _HWND["value"]
        deadline = time.time() + timeout
        while not hwnd and time.time() < deadline:
            hwnd = get_hwnd(window)
            if not hwnd:
                time.sleep(0.2)
        if not hwnd:
            log("钉窗口：拿不到窗口句柄，位置/大小只能交给 pywebview"
                "（缩放不是 100%% 的机器上可能偏大）")
            return None
        landed = apply_window_geometry(hwnd, x, y, width, height)
        log(describe_pin_target(x, y, width, height, landed))
        return landed
    except Exception:
        log("钉窗口这一步出错（不影响使用）：\n%s" % traceback.format_exc())
        return None


def get_hwnd(window, refresh=False):
    """拿到窗口的原生句柄（拿不到返回 None）。取到之后会缓存，之后直接读缓存。"""
    if _HWND["value"] and not refresh:
        return _HWND["value"]
    if window is None:
        return None
    try:
        native = window.native
        try:
            hwnd = int(native.Handle.ToInt64())
        except Exception:
            hwnd = int(native.Handle)
        if hwnd:
            _HWND["value"] = hwnd
            return hwnd
    except Exception:
        pass
    return None


def geometry_watch(window, stop_event):
    """
    盯着窗口位置和大小，变了就记下来。
    - 不用事件回调（事件名各版本不一致），直接隔一会儿量一次，更皮实。
    - **只读缓存好的句柄，绝不碰 window.native**：那是跨线程访问 GUI 对象，
      有过卡住的先例。句柄由 worker 线程取一次放进缓存。
    """
    last = None
    while not stop_event.is_set():
        try:
            hwnd = _HWND["value"]
            if hwnd:
                user32 = ctypes.windll.user32
                # ⚠️ 窗口最小化的时候，**什么都不要记**。
                # Windows 会把最小化窗口的位置报成 -32000,-32000，尺寸也变成
                # 一个没有意义的小值（约 160x28）。存下来就会把用户真正的窗口位置
                # 和大小冲掉 —— 2026-09-22 实测踩到：一次最小化之后，下次启动
                # 位置落到屏幕外、大小掉到最小尺寸。
                if user32.IsIconic(handle(hwnd)):
                    stop_event.wait(1.5)
                    continue
                rect = wt.RECT()
                user32.GetWindowRect(handle(hwnd), ctypes.byref(rect))
                maximized = bool(user32.IsZoomed(handle(hwnd)))
                if maximized:
                    # 最大化时 GetWindowRect 给的是最大化后的尺寸，
                    # 不能拿它当「用户想要的窗口大小」，只记最大化这个状态。
                    current = (None, None, None, None, True)
                else:
                    current = (rect.left, rect.top,
                               rect.right - rect.left, rect.bottom - rect.top, False)
                if current != last:
                    last = current
                    state = load_window_state()
                    state["maximized"] = current[4]
                    if current[0] is not None:
                        state["x"] = current[0]
                        state["y"] = current[1]
                        state["w"] = current[2]
                        state["h"] = current[3]
                    save_window_state(state)
        except Exception:
            pass
        stop_event.wait(1.5)


# --------------------------------------------------------------------------- #
# 新外壳：pywebview + WebView2
# --------------------------------------------------------------------------- #
def apply_webview_settings(webview):
    """
    改 pywebview 的默认设置。**这几个默认值会直接弄坏现有功能**，不能不管：

      ALLOW_DOWNLOADS     默认 False ->「导出 JSON / CSV / 导入模板」点了没反应
      OPEN_EXTERNAL_LINKS_IN_BROWSER 默认已经是 True -> 投递链接、学校详情页交给系统浏览器
      text_select         在 create_window 里，默认 False -> 选不中文字、复制不了
      private_mode        在 start 里，默认 True -> 每次启动清空存储，主题选择会丢
    """
    wanted = {
        "ALLOW_DOWNLOADS": True,
        "OPEN_EXTERNAL_LINKS_IN_BROWSER": True,
        "ALLOW_FILE_URLS": True,
    }
    for key in sorted(wanted):
        try:
            webview.settings[key] = wanted[key]
            log("外壳设置 %s = %r  (已生效)" % (key, wanted[key]))
        except Exception as exc:
            log("外壳设置 %s 改不了：%r  —— 相关功能可能不正常" % (key, exc))
    try:
        snapshot = dict(webview.settings)
        log("外壳设置快照：ALLOW_DOWNLOADS=%r  OPEN_EXTERNAL_LINKS_IN_BROWSER=%r"
            % (snapshot.get("ALLOW_DOWNLOADS"),
               snapshot.get("OPEN_EXTERNAL_LINKS_IN_BROWSER")))
    except Exception:
        pass


def run_pywebview_shell(config, port):
    """用 pywebview 建窗口，返回退出码。"""
    try:
        import webview
    except ImportError:
        log("没有 pywebview，退回 Edge --app 方式")
        alert("没有找到 pywebview，这个版本需要它。\n\n"
              "请运行一次：\n  pip install pywebview\n\n"
              "本次先用旧方式打开窗口。")
        return run_browser_shell(config, port)

    # pywebview 自己没有 __version__（探测时发现是空），版本号得从包元数据里读。
    version = getattr(webview, "__version__", None)
    if not version:
        try:
            import importlib.metadata as metadata
            version = metadata.version("pywebview")
        except Exception:
            version = "未知"
    log("pywebview 版本：%s" % version)
    apply_webview_settings(webview)

    if not os.path.isdir(WEBVIEW_PROFILE_DIR):
        try:
            os.makedirs(WEBVIEW_PROFILE_DIR)
        except Exception:
            log("建 webview_profile 失败：\n%s" % traceback.format_exc())

    width, height, x, y, maximized = startup_geometry()
    mode = str(config.get("window_mode") or "app").strip().lower()
    kiosk = mode == "kiosk"
    borderless = mode == "borderless"
    scale = GEOMETRY.get("scale") or 1.0

    splash_html = read_splash_html()
    kwargs = {
        # ⚠️ 传给 pywebview 的一律是**逻辑像素**：它建窗口时会再乘一次那台显示器的缩放
        #（winforms.py:198-220）。缩放 150% 时传物理像素过去会得到 1.5 倍大的窗口
        #（2026-09-27 双屏那台机器上报的"窗口被拉长、溢出到副屏"就是这个）。
        # 建完窗口还会用 apply_window_geometry 按物理像素钉一次，双保险。
        "width": DEFAULT_WINDOW_WIDTH if kiosk else winenv.to_logical(width, scale),
        "height": DEFAULT_WINDOW_HEIGHT if kiosk else winenv.to_logical(height, scale),
        "min_size": (winenv.to_logical(MIN_WINDOW_WIDTH, scale),
                     winenv.to_logical(MIN_WINDOW_HEIGHT, scale)),
        "text_select": True,             # 默认 False，会让页面上的字选不中、复制不了
        "background_color": "#f4f5fa",   # 和页面底色一致，少一次白闪
    }
    if x is not None and y is not None:
        kwargs["x"] = winenv.to_logical(x, scale)
        kwargs["y"] = winenv.to_logical(y, scale)
    # ⚠️ 无边框模式下**不能一开始就把窗口建成最大化**。
    #
    # 原因（2026-09-22 实际踩到）：最大化的窗口尺寸是 Windows 按「没有厚边框」算好的；
    # 我们又在下一条给窗口补 WS_THICKFRAME，客户区被边框挤小，多出来的那条不会重画，
    # 表现为窗口顶上一道黑框（日志里的特征：补丁时样式带 0x01000000 = WS_MAXIMIZE）。
    #
    # 正确顺序是：先正常建窗口 -> 补边框 -> 再最大化（这时 Windows 会按新边框重算尺寸）。
    want_maximized = bool(maximized)
    if want_maximized and not borderless:
        kwargs["maximized"] = True
    if kiosk or borderless:
        kwargs["fullscreen"] = True
        kwargs["frameless"] = True
    if borderless:
        # 拖动交给页面上的拖动条 + 我们自己的 Win32 调用。
        # 这里必须关掉 easy_drag：实测它会「松开鼠标后窗口还黏着光标」。
        kwargs.pop("fullscreen", None)
        kwargs["easy_drag"] = False

    # 窗口控制走 HTTP，不用 pywebview 的 js_api 桥（原因见 start_control_server 的说明）。
    # 必须在建窗口之前把端口开出来，因为端口要写进页面地址里。
    control_port, shell_api = start_control_server()

    log("建窗口：%sx%s  位置 %s,%s  最大化=%s  模式=%s  启动画面=%s"
        % (kwargs["width"], kwargs["height"], x, y,
           kwargs.get("maximized", False), mode, bool(splash_html)))

    try:
        if splash_html:
            window = webview.create_window(WINDOW_TITLE, html=splash_html, **kwargs)
        else:
            window = webview.create_window(WINDOW_TITLE, **kwargs)
    except Exception:
        log("建窗口失败：\n%s" % traceback.format_exc())
        alert("窗口没能建起来，详情见日志：\n%s" % LOG_PATH)
        return 1

    if shell_api is not None:
        shell_api.window = window
    stop_event = threading.Event()
    threading.Thread(target=geometry_watch, args=(window, stop_event), daemon=True).start()

    # 启动步骤的进度。卡住时靠它知道停在哪一步（只写日志和弹框，不改行为）。
    progress = {"step": "刚起步", "done": False}

    def step(text):
        progress["step"] = text
        log("  · " + text)

    def guard():
        """
        兜底看门狗：准备步骤要是卡住了，别让用户对着启动画面一直干等。
        到点就写日志 + 弹框 + 把窗口关掉，把「卡死」变成「看得见的报错」。
        """
        waited = 0
        while waited < 60 and not progress["done"]:
            time.sleep(1.0)
            waited += 1
        if progress["done"]:
            return
        log("准备步骤超过 60 秒没走完，停在：%s" % progress["step"])
        alert("程序启动卡在「%s」这一步。\n\n日志里有详细线索：\n%s"
              % (progress["step"], LOG_PATH))
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=guard, daemon=True).start()

    def worker(win):
        """
        窗口显示启动画面之后，这边同时去把服务准备好，再把窗口切到真实页面。
        放在这个线程里做，是为了让启动画面**先出现** —— 不然用户要先干等几秒。

        ⚠️ 顺序（2026-10-10 定）：**先接管/起后端，再打无边框补丁**。
        原来是反的：补丁要等窗口句柄才动得了手（`apply_borderless_when_ready` 里在轮询），
        实测从进程启动到「探测端口」要 6–9 秒。于是"关掉窗口后马上再双击"这条路要
        6–9 秒后才去问"端口上那个后端还能不能接管"——而后端收到关闭信号后的宽限只有
        15 秒（`services/lifecycle.CLOSE_GRACE_SECONDS`）。再叠上 shell 解析桌面快捷方式
        本身的那点延迟（这台电脑实测那次约 10 秒），接管就会晚于宽限到期、白白退化成
        "自己起一个新后端"（能用，但多等几秒，也不是无缝接管）。
        补丁是**装饰性**的（拖动/圆角/那圈黑边），后端是**功能性**的：功能性的先做。
        """
        # ① 先把后端准备好：能接管就接管（同一个进程继续给新窗口服务），
        #    不能接管（上一个后端已经在退 / 不认识这个接口 / 端口被别的程序占着）
        #    就等端口真空后自己起一个。**绝不复用一个"正在退出"的后端**。
        try:
            step("探测端口 %d（看程序是不是已经在跑）" % port)
            actual = reusable_backend(read_runtime_port() or port, control_port)
            step("端口探测完成：%s" % (actual or "没有服务在跑"))
            if actual is None:
                step("找一个可用的 Python")
                python_exe = find_python(config)
                log("使用解释器：%s" % python_exe)
                step("拉起后台服务")
                start_server(python_exe, config, control_port)
                step("等后台服务就绪")
                actual = wait_for_port(port)
                if actual and not attach_backend(actual, control_port):
                    actual = None
            if not actual:
                log("等待服务启动超时")
                progress["done"] = True
                alert("程序没能启动起来。\n\n请看这个日志文件里的原因：\n%s" % LOG_PATH)
                try:
                    win.destroy()
                except Exception:
                    pass
                return
            log("服务已就绪：http://127.0.0.1:%d/" % actual)
            url = "http://127.0.0.1:%d/" % actual
            if borderless:
                # 页面靠这个参数知道「我在自绘外壳里」，才会显示自绘标题栏。
                # 用普通浏览器打开时没有这个参数，页面就是原来的样子。
                url += "?shell=borderless"
            if control_port:
                url += ("&" if "?" in url else "?") + "shport=%d" % control_port
        except Exception:
            log("准备服务时异常：\n%s" % traceback.format_exc())
            progress["done"] = True
            alert("后台暂时无法连接。请稍候重新打开程序。\n详情见日志：\n%s" % LOG_PATH)
            try:
                win.destroy()
            except Exception:
                pass
            return

        # ② 再补外壳的装饰（无边框、圆角、黑边、最大化）
        patch_ok = False
        if borderless:
            # 无边框窗口缺的拖动/缩放/圆角/阴影，在这里补回去。
            # 必须放在这个线程里：窗口句柄要等 GUI 真正跑起来才存在。
            step("打无边框补丁")
            try:
                if apply_borderless_when_ready(win):
                    patch_ok = True
                    log("无边框模式就绪")
                    # 再把「非客户区」去掉。不做这一步的话，窗口四周会留着 7 像素的
                    # 边框区域，Windows 把那块渲染成透出桌面（带模糊），深色壁纸上
                    # 看着就是一道黑框 —— 2026-09-22 用像素测量确认的。
                    hook_window_proc(_HWND["value"])
            except Exception:
                # 补丁失败也要继续把程序开起来，不能因为装饰性的东西卡住启动
                log("无边框补丁抛异常（继续启动）：\n%s" % traceback.format_exc())
            # 补完边框、窗口尺寸已经是「带厚边框」的算法了，这时候再最大化才不会出黑框。
            # （顺序反过来的话，顶上会多一道黑边 —— 2026-09-22 就是这么踩的。）
            #
            # ⚠️ 顺序：**先按物理像素钉矩形、再最大化**。反过来的话钉矩形会把最大化顶掉。
            # 非无边框模式没有那段等待句柄的逻辑，也在这里一起钉（同样是为了缩放机器）。
            #
            # ⚠️ 下面这几段**必须各自包 try**：它们都在 worker 线程里，而 worker 一抛异常
            # 就再也没人往下走（服务不会起、页面不会切），用户看到的是"卡在启动画面"，
            # 60 秒后由看门狗报一句"卡在某一步"—— 2026-09-27 就是这么卡了整整一轮
            #（一句日志里的 `%d` 碰上 None）。装饰性的东西绝不能拖垮启动。
            try:
                if not kiosk and not want_maximized:
                    pin_window_geometry(win, x, y, width, height)
                # 硬兜底（2026-09-27）：无边框模式下**只要控件链缺了任何一环**，
                # 就不给用户一个"拖不动也关不掉"的窗口，改成带系统标题栏的（等价安全模式）。
                #   ① 补丁没打上：WS_THICKFRAME 没加上 → 拉边改大小、圆角、阴影全没有，
                #      还得留着那圈 7 像素黑框；32 位 Python 上就是这么全废的
                #      （`GetWindowLongPtrW` 在那台机器上不存在，见 pick_window_long_procs）；
                #   ② 拿不到窗口句柄：拖动/缩放/最大化全部静默失效（最小化还会假装成功）。
                # `relaunch_safe_mode` 带 `--bridge-fallback` 标记，不会无限重开。
                if borderless and (not patch_ok or not _HWND["value"]):
                    log("⚠️ 无边框外壳没弄好（补丁=%s，句柄=%s）：拖动/最小化/最大化会用不了"
                        % ("成功" if patch_ok else "失败", _HWND["value"] or "拿不到"))
                    relaunch_safe_mode("无边框补丁失败或拿不到窗口句柄（窗口控件会失效）")
                    return
            except Exception:
                log("摆窗口这一步出错（继续启动，不影响用）：\n%s" % traceback.format_exc())
            if want_maximized:
                try:
                    setup_win32()
                    hwnd = _HWND["value"]
                    if hwnd:
                        ctypes.windll.user32.ShowWindow(handle(hwnd), SW_MAXIMIZE)
                        log("已按上次的状态最大化窗口")
                except Exception:
                    log("最大化窗口失败（不影响使用）：\n%s" % traceback.format_exc())

        # ②.5 装饰做完了，确认窗口**真的在屏幕上**（2026-10-10 加的兜底）：
        # 无边框补丁可能跑在 pywebview「显示窗口」之前（补丁那行的样式会是 0x06010000，
        # 没有 WS_VISIBLE 位），那样窗口会停在"建好了但没显示"，屏幕上什么都没有、
        # 日志却一切正常——原始事故里最难查的一环。见 ensure_window_visible() 的说明。
        ensure_window_visible(_HWND["value"])
        # 顺手把程序图标设上：不设的话任务栏/Alt-Tab 用的是 pythonw 的 Python 图标，
        # 看起来就像"图标消失了"（见 apply_window_icon 的说明）。
        apply_window_icon(_HWND["value"])

        # ③ 后端已经就绪（url 在上面那段就算好了），把窗口从启动画面切到真实页面。
        try:
            step("把窗口切到真实页面")
            PAGE_RENDERED.clear()
            win.load_url(url)
            log("已把窗口切到真实页面：%s" % url)
            watch_first_render(win, url)
            # 同时盯「窗口控件那条通道通没通」：不通就用带系统标题栏的方式重开
            # （见 watch_shell_bridge 的说明）。只在自绘外壳下才有意义。
            if borderless:
                watch_shell_bridge()
            progress["done"] = True
        except Exception:
            log("切页面失败：\n%s" % traceback.format_exc())

    log("开始 pywebview 主循环")
    started = time.time()
    try:
        webview.start(worker, (window,), private_mode=False,
                      storage_path=WEBVIEW_PROFILE_DIR)
    except Exception:
        log("pywebview 主循环异常：\n%s" % traceback.format_exc())
        alert("窗口运行出错，详情见日志：\n%s" % LOG_PATH)
        return 1
    finally:
        stop_event.set()

    log("窗口已关闭，共开了 %.1f 秒" % (time.time() - started))

    # 告诉服务「页面正在关闭」。服务那边会自己判断：
    # 后台正在同步宣讲会时不会立刻退，会等活干完（跟 2.0 的行为一致）。
    #
    # ⚠️ 这里必须带上 &shport=<外壳端口>。服务端只认「窗口那个页面」发的关闭信号
    # （server.py 里 `if SHELL_PORT and source_port != SHELL_PORT: return False`），
    # 而这个端口正是本进程 start_control_server() 开出来的那个。
    # 2026-09-22 给服务端加来源校验时**漏改了这里**：请求一直是不带查询串的
    # `POST /api/closing`，于是被服务端判为「别的页面」直接忽略；
    # 日志却写「已通知服务」——因为 _raw_post 只要 recv() 不报错就返回 True，不看状态码。
    # 结果真正让程序退出的一直是页面 beforeunload 的 sendBeacon（那条带 shport）。
    # 只修一边不够：两条路都得真的通。control_port 为 0（控制服务没起来）时不带，
    # 此时服务端 SHELL_PORT 也是 0、本来就不校验，行为与改前一致。
    actual_port = read_runtime_port() or port
    closing_path = "/api/closing"
    if control_port:
        closing_path += "?shport=%d" % control_port
    if post(actual_port, closing_path):
        log("已通知服务：页面正在关闭（shport=%s）" % (control_port or "无"))
    else:
        log("通知服务关闭时没有响应（可能已经退了）")
    return 0


# --------------------------------------------------------------------------- #
# 老外壳：Edge / Chrome 的 --app 模式（config 里 shell 写 "browser" 时使用）
# --------------------------------------------------------------------------- #
def find_browser(config):
    fixed = (config.get("browser") or "").strip()
    if fixed and os.path.isfile(fixed):
        return fixed
    for path in BROWSER_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def open_app_window(browser, url, config):
    """用 --app 模式开窗口：没有地址栏、没有标签页。"""
    kiosk = str(config.get("window_mode") or "").strip().lower() == "kiosk"
    args = [
        browser,
        "--app=" + url,
        "--no-first-run",
        "--no-default-browser-check",
        "--user-data-dir=" + BROWSER_PROFILE_DIR,
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-client-side-phishing-detection",
        "--disable-domain-reliability",
        "--no-service-autorun",
        "--no-pings",
        "--metrics-recording-only",
        "--disable-breakpad",
        "--disable-features=Translate,msEdgeIdentityIntegration,WebUIDarkMode",
    ]
    if kiosk:
        args.append("--kiosk")
        log("窗口模式：kiosk（全屏无边框）")
    else:
        args.append("--window-size=%d,%d" % (DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT))
        log("窗口模式：app（带标题栏的独立窗口）")
    subprocess.Popen(args, close_fds=True)


def run_browser_shell(config, port):
    browser = find_browser(config)
    if not browser:
        log("没有找到 Edge 或 Chrome")
        alert("没有找到 Edge 或 Chrome，无法打开窗口。\n"
              "请先安装任意一个浏览器（Windows 一般自带 Edge）。")
        return 1

    if ping(port):
        actual = read_runtime_port() or port
        log("检测到程序已在运行，直接开窗口：%d" % actual)
        open_app_window(browser, "http://127.0.0.1:%d/" % actual, config)
        return 0

    python_exe = find_python(config)
    log("使用解释器：%s" % python_exe)
    start_server(python_exe, config)
    actual = wait_for_port(port)
    if not actual:
        log("等待程序启动超时")
        alert("程序没能启动起来。\n\n请看这个日志文件里的原因：\n%s" % LOG_PATH)
        return 1
    log("服务已就绪：http://127.0.0.1:%d/" % actual)
    open_app_window(browser, "http://127.0.0.1:%d/" % actual, config)
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def relaunch_windowed(config):
    """
    这次是被**带控制台**的 python.exe 拉起来的话，换成同目录的 pythonw.exe 重开一份，自己退出。

    为什么要这么做：Windows 会给控制台进程配一个黑窗口，而**关掉那个黑窗口会把整个进程组一起
    带走** —— 用户看到的就是「关掉黑窗口，程序跟着退了」。pythonw.exe 没有控制台，就没有东西可关。
    正常安装走的是快捷方式（pythonw.exe），永远用不到这里；这是给「不知道怎么把这个程序拉起来」
    的情况兜底（试用包、或者谁直接双击了 launcher.py）。

    只在 config.json 里开了 relaunch_windowed 时才动手：用户自己的安装没这个开关，
    「启动（调试窗口）.cmd」也要保留控制台输出，不能被悄悄换掉。
    """
    if not config.get("relaunch_windowed"):
        return False
    if "--windowed-restart" in sys.argv[1:]:
        return False
    if os.path.basename(sys.executable).lower() != "python.exe":
        return False
    beside = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(beside):
        return False
    here = os.path.dirname(os.path.abspath(__file__))
    args = [beside, os.path.abspath(__file__)] + sys.argv[1:] + ["--windowed-restart"]
    try:
        subprocess.Popen(
            args, cwd=here, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    except OSError as exc:
        log("本来想换成无控制台的方式重开，但没成功（%s），就地继续" % exc)
        return False
    log(">>> 这次是被带控制台的 python.exe 拉起来的，已改用 pythonw.exe 重开一份"
        "（否则那个黑窗口一关，程序会跟着退出）")
    return True


# --------------------------------------------------------------------------- #
# 桌面快捷方式（2026-09-27 加）
#
# 为什么要有它：试用包/升级包发出去之后，对方是「解压到一个文件夹、双击 启动.cmd」，
# 桌面上什么都没有 —— 每次开机都得翻回那个文件夹去找。开发者自己那份是手工建的快捷方式，
# 所以这个缺口一直没暴露（用户 2026-09-27 反馈「试用版没有自动生成快捷方式」）。
#
# 两条不能破的性质：
#   ① **只在缺失时建，绝不覆盖已有的** —— 用户可能自己改过图标/名字/目标，不能给他改回去；
#   ② **绝不影响启动** —— 整个动作在后台线程里做，失败只往 launcher.log 写一行。
#      「建不成」的原因很多（组策略禁 PowerShell、桌面被重定向、权限、杀软），
#      任何一种都不该让程序起不来，也不该弹框打扰人。
#
# 为什么不先判「个人版 / 正式版」：升级上来的安装 **`config.json` 是保留用户那份的**，
# 里头没有 `build` 键，`detect_edition()` 会按「有数据库」判成 personal ——
# 而那正是**最需要**这个快捷方式的人（从老试用版升上来的同学）。
# 所以判断依据换成事实：**桌面上有没有这个快捷方式**。开发者自己那份桌面上已经有了（同名），
# 这条规则天然不会去动它。
# --------------------------------------------------------------------------- #
SHORTCUT_NAME = "应届海投管理.lnk"
SHORTCUT_DESC = "应届海投管理 —— 求职进度管理"
CREATE_NO_WINDOW = 0x08000000


def _powershell_exe():
    """Windows PowerShell 的绝对路径（优先 5.1，它一定在 System32 下）。"""
    candidate = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                             "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return candidate if os.path.isfile(candidate) else "powershell"


def shortcut_launch_command():
    """
    快捷方式该指向什么：返回 `(target, arguments)`；算不出来返回 None。

    **只认「不会弹黑窗口」的两种启动方式** —— 控制台 python 会带一个黑窗口，
    而关掉那个黑窗口等于给整个进程组发关闭信号，程序跟着退（真踩过）。
      ① 自己就是 pythonw.exe（正常情况）-> 直接用它 + launcher.py；
      ② 有同目录的 pythonw.exe（调试窗口那条路）-> 用那个；
      ③ 一个都没有（`启动.cmd` 的 HIDDEN 兜底）-> wscript + 包里的 `_hidden_start.vbs`。
    三条都不成立时宁可不建 —— 也不建一个"点了会弹黑窗、关掉还会把程序带走"的东西。
    """
    launcher = os.path.join(BASE_DIR, "launcher.py")
    exe = sys.executable or ""
    if exe.lower().endswith("pythonw.exe") and os.path.isfile(exe):
        return exe, '"%s"' % launcher
    sibling = os.path.join(os.path.dirname(exe), "pythonw.exe") if exe else ""
    if sibling and os.path.isfile(sibling):
        return sibling, '"%s"' % launcher
    vbs = os.path.join(BASE_DIR, "_hidden_start.vbs")
    if exe and os.path.isfile(exe) and os.path.isfile(vbs):
        wscript = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                               "System32", "wscript.exe")
        return wscript, '"%s" "%s" "%s"' % (vbs, exe, launcher)
    return None


def sync_shell_icon():
    """
    把程序内部的图标同步一份到「壳读得到」的位置（`%LOCALAPPDATA%\\ResumeTracker\\app.ico`），
    返回快捷方式该用的图标路径。任何一步失败都退回程序内部那份，不影响启动。

    为什么需要多这一份：见 `SHELL_ICON` 上面的注释 —— 程序目录那棵树可能带低完整性标签，
    Explorer 从那里取快捷方式图标会失败（2026-10-10 用 IShellItemImageFactory 逐个对照
    才定位到：同一个文件在带沙箱标签的程序目录子树里显示通用图，复制到用户目录下就正常）。
    这一份是**自愈**的：每次启动比大小和修改时间，不一致就覆盖；用户删了下次启动补回来。
    """
    try:
        if not os.path.isfile(APP_ICON):
            return ""
        if os.path.abspath(SHELL_ICON) == os.path.abspath(APP_ICON):
            return APP_ICON
        stale = (not os.path.isfile(SHELL_ICON)
                 or os.path.getsize(SHELL_ICON) != os.path.getsize(APP_ICON)
                 or abs(os.path.getmtime(SHELL_ICON) - os.path.getmtime(APP_ICON)) > 1)
        if stale:
            folder = os.path.dirname(SHELL_ICON)
            if not os.path.isdir(folder):
                os.makedirs(folder)
            with open(APP_ICON, "rb") as src:
                data = src.read()
            with open(SHELL_ICON, "wb") as dst:
                dst.write(data)
            log("快捷方式图标：已同步一份到 %s（壳只肯从这类位置取图标）" % SHELL_ICON)
        return SHELL_ICON
    except Exception:
        log("快捷方式图标：同步失败，退回程序内部那份（不影响使用）：\n%s" % traceback.format_exc())
        return APP_ICON


def shortcut_script(name, target, arguments, icon, legacy_icon=""):
    """
    拼出建快捷方式的 PowerShell 脚本（纯字符串，方便单独验）。

    桌面的位置**由 PowerShell 自己问系统**（`GetFolderPath('Desktop')`）——
    桌面可能被重定向到 OneDrive，自己拼 `%USERPROFILE%\\Desktop` 会指到空目录。
    自检可以设 `RESUME_TRACKER_DESKTOP` 指到别处（不设就是真桌面，行为不变）。
    """
    # 图标值：**不要加引号**。实测给带空格的路径加引号，Explorer 会把引号当成文件名的一部分
    # 从而取不到图标（路径里有空格本身没事 —— 2026-10-10 逐项对照过）。索引 `,0` 是标准写法。
    set_icon = ("$s.IconLocation='%s,0';" % icon) if icon else ""
    # 已存在时**绝不覆盖**（用户可能自己改过名字/目标/图标）。唯一例外：**我们自己写坏的那条** ——
    #   ① 图标是空的；② 图标正好等于「程序内部那份」（1.1.8 及以前就这么写，壳读不到 ⇒ 显示通用图）。
    # 命中这两种就只改图标、别的一个字不动，退出码 5。
    repair = ""
    if icon and legacy_icon:
        repair = ("if ((-not $cur) -or ($cur -eq '%s') -or ($cur -eq '%s,0')) {"
                  "$s.IconLocation='%s,0';$s.Save();exit 5};"
                  % (legacy_icon, legacy_icon, icon))
    return (
        "$ErrorActionPreference='Stop';"
        "$d=$env:RESUME_TRACKER_DESKTOP;"
        "if (-not $d) { $d=[Environment]::GetFolderPath('Desktop') };"
        "if (-not $d -or -not (Test-Path -LiteralPath $d)) { exit 3 };"
        "$lnk=Join-Path $d '%s';"
        "if (Test-Path -LiteralPath $lnk) {"
        "$sh=New-Object -ComObject WScript.Shell;$s=$sh.CreateShortcut($lnk);$cur=$s.IconLocation;"
        "%s"
        "exit 4 };"
        "$sh=New-Object -ComObject WScript.Shell;"
        "$s=$sh.CreateShortcut($lnk);"
        "$s.TargetPath='%s';"
        "$s.Arguments='%s';"
        "$s.WorkingDirectory='%s';"
        "%s"
        "$s.Description='%s';"
        "$s.Save()"
    ) % (name, repair, target, arguments, BASE_DIR, set_icon, SHORTCUT_DESC)


def ensure_desktop_shortcut():
    """
    桌面上没有这个快捷方式就补一个。**后台线程里跑，任何情况都不许影响启动。**

    退出码约定（PowerShell 侧）：0 = 刚建好；4 = 本来就有（**不动它**）；
    5 = 本来就有、但图标是我们自己写坏的那种（只把图标修回来）；
    3 = 找不到桌面目录；其它 = 真出错。统一只在 launcher.log 留一行。
    """
    try:
        command = shortcut_launch_command()
        if not command:
            log("快捷方式：拿不到「不弹黑窗口」的启动方式，跳过（不影响使用）")
            return False
        target, arguments = command
        # 图标用「壳读得到的那一份」（sync_shell_icon 会维护它，见 SHELL_ICON 的说明）；
        # legacy_icon 传程序内部那份，好让老快捷方式（1.1.8 写的是它）能被修回来。
        icon = sync_shell_icon()
        script = shortcut_script(SHORTCUT_NAME, target, arguments, icon,
                                 legacy_icon=APP_ICON)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        # 走 -EncodedCommand（UTF-16LE + Base64）：脚本里有中文和中文路径，
        # 直接写在命令行里会踩 cmd.exe / 控制台代码页的坑（（开发期的一次性脚本） 那次的办法）。
        # 输出丢 DEVNULL 而不是 capture_output —— 不要管道（受限环境里管道会直接 EPERM，
        # 而且这里只需要退出码）。
        proc = subprocess.run(
            [_powershell_exe(), "-NoProfile", "-NonInteractive",
             "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30, creationflags=CREATE_NO_WINDOW)
        if proc.returncode == 0:
            log("快捷方式：刚在桌面建好（%s -> %s）" % (SHORTCUT_NAME, target))
            return True
        if proc.returncode == 4:
            log("快捷方式：桌面上本来就有，不动它")
            return True
        if proc.returncode == 5:
            log("快捷方式：把老快捷方式的图标修到了 %s（原来那个位置壳读不到）" % icon)
            return True
        log("快捷方式：没建成（退出码 %s，不影响使用）" % proc.returncode)
        return False
    except Exception as exc:
        log("快捷方式：跳过（%r，不影响使用）" % (exc,))
        return False


# --------------------------------------------------------------------------- #
# 桌面快捷方式（2026-09-27 加）
#
# 为什么要有它：试用包/升级包发出去之后，对方是「解压到一个文件夹、双击 启动.cmd」，
# 桌面上什么都没有 —— 每次开机都得翻回那个文件夹去找。开发者自己那份是手工建的快捷方式，
# 所以这个缺口一直没暴露（用户 2026-09-27 反馈「试用版没有自动生成快捷方式」）。
#
# 两条不能破的性质：
#   ① **只在缺失时建，绝不覆盖已有的** —— 用户可能自己改过图标/名字/目标，不能给他改回去；
#   ② **绝不影响启动** —— 整个动作在后台线程里做，失败只往 launcher.log 写一行。
#      「建不成」的原因很多（组策略禁 PowerShell、桌面被重定向、权限、杀软），
#      任何一种都不该让程序起不来，也不该弹框打扰人。
#
# 为什么不先判「个人版 / 正式版」：升级上来的安装 **`config.json` 是保留用户那份的**，
# 里头没有 `build` 键，`detect_edition()` 会按「有数据库」判成 personal ——
# 而那正是**最需要**这个快捷方式的人（从老试用版升上来的同学）。
# 所以判断依据换成事实：**桌面上有没有这个快捷方式**。开发者自己那份桌面上已经有了（同名），
# 这条规则天然不会去动它。
def main():
    from services import update_txn
    allowed, reason = update_txn.check_startup(BASE_DIR)
    if not allowed:
        app_paths.record_startup_error(reason)
        log(reason)
        return 2
    config = load_config()
    # ⚠️ 第一件事：声明「逐显示器 DPI 感知」（2026-09-27 加）。
    # 必须在**读任何屏幕/窗口尺寸之前**做：不感知的进程拿到的是被 Windows 虚拟化的坐标，
    # 缩放 150% 的屏上跟真实坐标差 1.5 倍 —— 双屏那台机器"窗口被拉长、溢出到副屏"就是这么来的。
    # Windows 只认第一次生效的调用，所以这里必须早于 pywebview 自己的 SetProcessDPIAware()。
    requested = winenv.enable_dpi_awareness()
    # 自检副本可以在 config.json 里写一个自己的 "mutex"，这样能和主程序同时开着各自测；
    # 平时没有这个键，走的就是默认名字，行为完全不变。
    custom_mutex = (config.get("mutex") or "").strip()
    if custom_mutex:
        global MUTEX_NAME
        MUTEX_NAME = custom_mutex
    port = int(config.get("port") or 8768)
    shell = str(config.get("shell") or "pywebview").strip().lower()
    log("=" * 60)
    log("启动器开始运行：外壳=%s 端口=%d 解释器=%s" % (shell, port, sys.executable))
    log("解释器位数：%d 位（%s）" % (ctypes.sizeof(ctypes.c_void_p) * 8, sys.version.split()[0]))
    log("DPI：声明=%s，实际=%s（显示器 %d 台，%d DPI）"
        % (requested, winenv.awareness_name(), winenv.display_count(), winenv.system_dpi()))
    if relaunch_windowed(config):
        return 0

    # 安全模式（学自 开发工具 Desktop 的 --safe-mode）：
    # 「启动（安全模式）.cmd」会带上这个参数，表示**不管 config.json 里怎么写的**，
    # 都用最保守的方式打开 —— 带系统标题栏的外壳，不碰无边框、不接管窗口消息。
    # 外壳层万一出问题（窗口没边框、拖不动、黑框之类），双击那个 cmd 就能回到能用的
    # 状态，不需要手改 JSON。用户数据完全不受影响。
    if "--safe-mode" in sys.argv[1:]:
        config = dict(config)
        config["shell"] = "pywebview"
        config["window_mode"] = "app"
        shell = "pywebview"
        log(">>> 安全模式：忽略 config.json 的外壳设置，用带系统标题栏的方式打开")

    if shell == "browser":
        log("按 config.json 的要求使用 Edge --app 外壳")
        return run_browser_shell(config, port)

    first, mutex_handle = acquire_single_instance()
    if not first:
        # 正常情况：上面已经把已有窗口提到前面了（focus_existing_window 里记了日志）。
        # 走到这里说明等了一会儿也没找到窗口 —— 极少见，提示一下。
        log("这次不再开新窗口")
        if not focus_existing_window():
            alert("已有程序尚未完成启动或退出。\n请稍候再双击；若仍无窗口，请用启动（安全模式）重试。")
        return 0

    # 桌面快捷方式（2026-09-27）：只在**缺**的时候补一个，后台线程做，绝不拦启动。
    # 只让拿到单实例锁的这一份做，省得两份同时去写同一个文件。
    threading.Thread(target=ensure_desktop_shortcut, daemon=True).start()

    code = run_pywebview_shell(config, port)
    try:
        if mutex_handle:
            ctypes.windll.kernel32.ReleaseMutex(mutex_handle)
            ctypes.windll.kernel32.CloseHandle(mutex_handle)
    except Exception:
        pass
    return code


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception:
        log("启动器异常：\n%s" % traceback.format_exc())
        alert("启动出错，详情见日志：\n%s" % LOG_PATH)
        exit_code = 1
    sys.exit(exit_code)
