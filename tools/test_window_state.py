# -*- coding: utf-8 -*-
"""
窗口状态读取的回归测试（纯逻辑，不需要开窗口）。

【为什么有它】
2026-09-22 出过一次事故：窗口最小化的时候，Windows 会把位置报成 -32000,-32000、
尺寸报成约 160x28。当时的记录逻辑没排除最小化状态，把它当成真实位置存了下来，
结果下次启动窗口位置落到屏幕外、大小掉到最小值 900x640
（还连带引出了「顶部黑框」那个问题）。

这个脚本把当时的几种坏输入固化下来，防止再犯。launcher.py 里对应的修复是：
  - geometry_watch()：最小化时什么都不记
  - startup_geometry()：尺寸不合理就用默认大小，而不是夹到最小值

用法：python work\\test_window_state.py
"""
import json
import os
import shutil
import sys
import tempfile

import ctypes

sys.dont_write_bytecode = True     # 别在 app\ 里留 __pycache__（下面要 import launcher）
sys.stdout.reconfigure(encoding="utf-8")

V3 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, V3)
import launcher  # noqa: E402
import winenv    # noqa: E402

ok = True


def check(label, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("[PASS] " if cond else "[FAIL] ") + label + ("  " + extra if extra else ""))


def run_case(label, state, expect):
    """把给定的状态写进临时文件，看 startup_geometry() 算出什么。"""
    path = tempfile.mktemp(suffix=".json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    old_path = launcher.STATE_PATH
    launcher.STATE_PATH = path
    try:
        got = launcher.startup_geometry()
    finally:
        launcher.STATE_PATH = old_path
        try:
            os.remove(path)
        except OSError:
            pass
    check(label, got == expect, "算出 %s，期望 %s" % (got, expect))


print("检查 launcher.py 的 startup_geometry()（窗口大小/位置怎么读）\n")

# 默认值：宽 1280、高 860、最小 900x640
run_case("正常记录：原样沿用",
         {"x": 100, "y": 100, "w": 1400, "h": 900, "maximized": False},
         (1400, 900, 100, 100, False))

run_case("最小化留下的坏位置被忽略",
         {"x": -32000, "y": -32000, "w": 160, "h": 28, "maximized": False},
         (1280, 860, None, None, False))

run_case("尺寸太小 -> 用默认大小，而不是夹到最小值",
         {"x": 100, "y": 100, "w": 160, "h": 28, "maximized": False},
         (1280, 860, 100, 100, False))

run_case("只有尺寸偏小一点点（小于最小尺寸）也走默认",
         {"x": 100, "y": 100, "w": 899, "h": 639, "maximized": False},
         (1280, 860, 100, 100, False))

run_case("空文件：全用默认",
         {},
         (1280, 860, None, None, False))

run_case("最大化标记会被保留（窗口何时最大化由调用方决定）",
         {"x": 100, "y": 100, "w": 1400, "h": 900, "maximized": True},
         (1400, 900, 100, 100, True))

run_case("位置在屏幕外但尺寸正常：只丢位置，保留尺寸",
         {"x": -32000, "y": 100, "w": 1400, "h": 900, "maximized": False},
         (1400, 900, None, None, False))

# --------------------------------------------------------------------------- #
# 2026-09-27 加：DPI / 多屏。来源是那台**双屏 + 缩放**的机器上报的
# 「窗口被拉长、约 1/5 溢出到副屏，最小化/最大化/拖动都点不动」。
# 根因是 pywebview 建窗口时会把它收到的宽高**当逻辑像素再乘一次缩放**
# （winforms.py:198-220），而我们存的是物理像素 —— 缩放 150% 时 1400x900 会被做成 2100x1350，
# 比屏幕还大。修法两条：算出来的矩形**夹进目标显示器的可用区域**，以及传给 pywebview 之前
# **换算成逻辑像素**、建完再用 SetWindowPos 按物理像素钉一次。
# --------------------------------------------------------------------------- #
print()
print("=" * 70)
print("DPI / 多屏：把窗口矩形夹进显示器可用区域（winenv.fit_to_work_area，纯计算）")
print("=" * 70)


def fit_case(label, x, y, w, h, work, expect, minw=900, minh=640):
    got = winenv.fit_to_work_area(x, y, w, h, work, minw, minh)
    check(label, got == expect, "算出 %s，期望 %s" % (got, expect))


# 单屏 1920x1080、任务栏占下面 40px（可用区域 1920x1040）
fit_case("单屏：正常矩形原样不动", 324, 190, 1280, 860, (0, 0, 1920, 1080),
         (324, 190, 1280, 860))
fit_case("底边压到任务栏：往上推", 324, 190, 1280, 860, (0, 0, 1920, 1040),
         (324, 180, 1280, 860))
# 缩放 150% 那台机器上报的形态：1400x900 被再乘 1.5 -> 2100x1350，比屏幕还大
fit_case("比屏幕还大：尺寸夹到可用区域、位置拉回屏幕内", 150, 150, 2100, 1350,
         (0, 0, 1920, 1080), (0, 0, 1920, 1080))
fit_case("右下溢出：往左上推回来", 1800, 1000, 1000, 700, (0, 0, 1920, 1080),
         (920, 380, 1000, 700))
fit_case("双屏（副屏在右）：留在副屏上，别拉回主屏", 2000, 100, 1280, 860,
         (1920, 0, 3840, 1040), (2000, 100, 1280, 860))
fit_case("双屏（副屏在左，负坐标）：原样保留", -1800, 50, 1280, 860,
         (-1920, 0, 0, 1040), (-1800, 50, 1280, 860))
fit_case("没有历史位置：在工作区居中", None, None, 1280, 860, (0, 0, 1920, 1080),
         (320, 110, 1280, 860))
fit_case("尺寸小于下限：抬到最小 900x640", 100, 100, 400, 300, (0, 0, 1920, 1080),
         (100, 100, 900, 640))
fit_case("屏幕比最小尺寸还窄：不许算出负数/越界", 0, 0, 1280, 860, (0, 0, 800, 600),
         (0, 0, 900, 640))
fit_case("任务栏在下：窗口底边不压到任务栏", 0, 900, 1280, 860, (0, 0, 1920, 1040),
         (0, 180, 1280, 860))

print()
print("=" * 70)
print("物理像素 -> pywebview 要的逻辑像素（winenv.to_logical）")
print("=" * 70)
check("100% 缩放：原样传", winenv.to_logical(1400, 1.0) == 1400,
      str(winenv.to_logical(1400, 1.0)))
check("150% 缩放：1400 -> 933（它再乘 1.5 ≈ 1400）", winenv.to_logical(1400, 1.5) == 933,
      str(winenv.to_logical(1400, 1.5)))
check("缩放传 0/None 时当 1.0，不许算出 0", winenv.to_logical(1400, 0) == 1400,
      str(winenv.to_logical(1400, 0)))
round_ok, worst = True, 0
for scale in (1.0, 1.25, 1.5, 1.75, 2.0):
    for physical in (900, 1280, 1400, 1920):
        back = winenv.to_logical(physical, scale) * scale
        worst = max(worst, abs(back - physical))
        if abs(back - physical) > scale:       # 允许 1 逻辑像素的取整误差
            round_ok = False
check("各种缩放下往返误差都不超过 1 逻辑像素", round_ok, "最大偏差 %.2f px" % worst)

print()
print("=" * 70)
print("DPI 感知本身（winenv.enable_dpi_awareness / awareness_name）")
print("=" * 70)
declared = winenv.enable_dpi_awareness()
check("能声明成功（返回一个级别名）", bool(declared), declared)
check("声明之后实际级别不再是 UNAWARE", "UNAWARE" not in winenv.awareness_name(),
      winenv.awareness_name())
dpi = winenv.system_dpi()
check("读得到系统 DPI（96 = 100%%，按 24 递增）", dpi >= 96 and dpi % 24 == 0, str(dpi))
work_now, scale_now = winenv.monitor_work_area()
check("读得到某台显示器的可用区域（宽高都 > 0）",
      work_now[2] > work_now[0] and work_now[3] > work_now[1], str(work_now))
check("读得到缩放倍数（1.0 ~ 4.0 之间）", 1.0 <= scale_now <= 4.0, str(scale_now))
check("显示器台数 >= 1", winenv.display_count() >= 1, str(winenv.display_count()))

# startup_geometry 真的把「过大尺寸」夹住了吗（对着这台电脑可用区域算期望，不写死数字）
_work, _ = winenv.monitor_work_area(100, 100)
_expect = winenv.fit_to_work_area(100, 100, 4000, 3000, _work, 900, 640)
run_case("缩放机器上的过大尺寸：被夹进这台电脑可用区域（不再溢出到别的屏）",
         {"x": 100, "y": 100, "w": 4000, "h": 3000, "maximized": False},
         (_expect[2], _expect[3], _expect[0], _expect[1], False))
check("startup_geometry 会把这台电脑缩放记进 GEOMETRY（后面换算逻辑像素要用）",
      isinstance(launcher.GEOMETRY.get("scale"), float) and launcher.GEOMETRY["scale"] > 0,
      "scale=%r" % (launcher.GEOMETRY.get("scale"),))

# 2026-09-27：全新安装（没有 window_state.json）时位置是 None —— "钉窗口"那一步
# 第一版直接 int(None) 崩了，**所有新用户第一次启动都会卡在启动画面**
# （开发者这台电脑早就有了 window_state.json，永远碰不到）。升级门当场抓到的。
print()
print("=" * 70)
print("全新安装：位置是 None 时该怎么处理（launcher.resolve_pin_position）")
print("=" * 70)
check("两个都是 None：沿用窗口当前的位置（不许 int(None)）",
      launcher.resolve_pin_position(None, None, 300, 200) == (300, 200),
      str(launcher.resolve_pin_position(None, None, 300, 200)))
check("只有一个 None：只补那一个", launcher.resolve_pin_position(50, None, 300, 200) == (50, 200),
      str(launcher.resolve_pin_position(50, None, 300, 200)))
check("都有值：原样返回（不理会当前值）",
      launcher.resolve_pin_position(10, 20, 300, 200) == (10, 20),
      str(launcher.resolve_pin_position(10, 20, 300, 200)))
# 2026-09-27：那行日志原来写成 `%d,%d`，全新安装时 x/y 是 None → TypeError，
# 而它不在任何 try 里 → worker 线程死掉 → 服务不起、卡在启动画面（升级门抓到的）。
check("日志能打印 None 位置（不许再用 %d 碰 None）",
      isinstance(launcher.describe_pin_target(None, None, 1280, 860, (26, 26, 1280, 860)), str),
      launcher.describe_pin_target(None, None, 1280, 860, (26, 26, 1280, 860)))
check("没有窗口/拿不到句柄时 pin_window_geometry 不抛异常（只记一行日志）",
      launcher.pin_window_geometry(None, None, None, 1280, 860, timeout=0.2) is None)

# --------------------------------------------------------------------------- #
# 2026-09-27（第二次）：**32 位 Python 上 user32 没有 `*PtrW` 导出**
#
# 试用的人那台机器是 `Python312-32`，日志里 `AttributeError: function 'GetWindowLongPtrW'
# not found` 刷了 124 次：`setup_win32()` 一开始就抛，于是无边框补丁失败 33 次、
# 拖动/最小化/最大化全部失效 —— 用户看到的就是"窗口拖不动、按钮点不动"。
# 这台电脑是 64 位，正常情况下永远复现不了，所以这里**把 user32 换成"32 位那一份"**来验。
# --------------------------------------------------------------------------- #
print()
print("=" * 70)
print("32 位 Python：user32 里没有 *PtrW 导出（模拟）")
print("=" * 70)


class _FakeUser32(object):
    """假的 user32：`has_ptr=False` 时**没有**那两 个 *PtrW 导出（32 位就是这个样子）。"""

    def __init__(self, has_ptr):
        if has_ptr:
            self.GetWindowLongPtrW = object()
            self.SetWindowLongPtrW = object()
        self.GetWindowLongW = object()
        self.SetWindowLongW = object()

    def __getattr__(self, name):        # 真 WinDLL 取不到导出也是抛 AttributeError
        raise AttributeError(name)


_fake64, _fake32 = _FakeUser32(True), _FakeUser32(False)


def _pick(fake):
    """调 pick_window_long_procs，但**把异常变成结果** —— 让"直接抛 AttributeError"
    这种情况显示成一条 [FAIL]，而不是把整个测试脚本崩掉（崩掉的话后面几条都不跑了）。"""
    try:
        return launcher.pick_window_long_procs(fake)
    except Exception as exc:
        return "抛了 %s: %s" % (type(exc).__name__, exc)


check("64 位那一份：优先用 *PtrW 版",
      _pick(_fake64) == (_fake64.GetWindowLongPtrW, _fake64.SetWindowLongPtrW),
      "%r" % (_pick(_fake64),))
check("32 位那一份：退回 W 版（不许直接抛 AttributeError）",
      _pick(_fake32) == (_fake32.GetWindowLongW, _fake32.SetWindowLongW),
      "%r" % (_pick(_fake32),))
check("两种名字都没有：返回 (None, None)，由调用方明确报错",
      _pick(object()) == (None, None), "%r" % (_pick(object()),))

_real_user32 = ctypes.windll.__dict__.get("user32")


class _NoPtrUser32(object):
    """把真实的 user32 包一层，但**藏掉** *PtrW —— 等于在这台 64 位机器上装出 32 位环境。"""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        if name in ("GetWindowLongPtrW", "SetWindowLongPtrW"):
            raise AttributeError("function '%s' not found" % name)
        return getattr(self._real, name)


ctypes.windll.__dict__["user32"] = _NoPtrUser32(_real_user32)
launcher._WIN32_READY["done"] = False
launcher._WIN_LONG["get"] = launcher._WIN_LONG["set"] = None
try:
    crashed = None
    try:
        launcher.setup_win32()
    except Exception as exc:                     # 修好之前这里就是 AttributeError
        crashed = exc
    check("模拟 32 位：setup_win32() 不再抛异常（修之前必炸）", crashed is None,
          "抛了 %r" % (crashed,))
    check("模拟 32 位：挑到的是 W 版那一个",
          launcher._WIN_LONG["get"] is _real_user32.GetWindowLongW
          and launcher._WIN_LONG["set"] is _real_user32.SetWindowLongW,
          "挑到 %r" % (launcher._WIN_LONG["get"],))
finally:
    ctypes.windll.__dict__["user32"] = _real_user32
    launcher._WIN32_READY["done"] = False
    launcher._WIN_LONG["get"] = launcher._WIN_LONG["set"] = None

launcher.setup_win32()
check("复原之后：这台电脑（%d 位）上那一对又挑得出来"
      % (ctypes.sizeof(ctypes.c_void_p) * 8),
      launcher._WIN_LONG["get"] is not None and launcher._WIN_LONG["set"] is not None)

# --------------------------------------------------------------------------- #
# 2026-09-28：「缺少 openpyxl」的真凶 —— 挑后端解释器时把 32 位的 Python 整个漏掉了
#
# 那台机器上装了 `Python39`（C:\Program Files）和 `Python312-32`（32 位安装的目录名
# 带 `-32` 后缀）。旧代码的目录名正则是 `^Python3\d+$`，**不认带后缀的**，
# 于是候选里只剩 Python39，后端被换到 3.9 —— 而依赖（pywebview/openpyxl）是
# `启动.cmd` 装在启动器自己用的那个 3.12-32 里的，界面就报「缺少 openpyxl」。
# 修法两条：① 目录名要认 `-32`；② **优先用"启动自己的那个解释器"**（依赖就在它里面）。
# --------------------------------------------------------------------------- #
print()
print("=" * 70)
print(u"挑解释器：认 Python312-32，且不换掉「装好依赖的那个」")
print("=" * 70)
check("目录名 Python312-32 认得出（旧正则把它整个跳过）",
      launcher.python_version_key(r"C:\x\Python312-32\pythonw.exe") == (12, 32),
      "%r" % (launcher.python_version_key(r"C:\x\Python312-32\pythonw.exe"),))
check("目录名 Python39 也认得出，且标成 64 位",
      launcher.python_version_key(r"C:\Program Files\Python39\pythonw.exe") == (9, 64),
      "%r" % (launcher.python_version_key(r"C:\Program Files\Python39\pythonw.exe"),))
_cands = [
    (launcher.python_version_key(r"C:\Program Files\Python39\pythonw.exe"), "Python39"),
    (launcher.python_version_key(r"C:\x\Python312-32\pythonw.exe"), "Python312-32"),
    (launcher.python_version_key(r"C:\x\Python311\pythonw.exe"), "Python311"),
]
_cands.sort(key=lambda item: item[0])
check("排序：版本号优先（3.12 最大，排在最后）",
      [name for _key, name in _cands] == ["Python39", "Python311", "Python312-32"],
      "%s" % [name for _key, name in _cands])
check("同版本时 64 位排前面",
      sorted([((12, 32), "32位"), ((12, 64), "64位")])[-1][1] == "64位")

# 真扫一遍假目录树：确认带 -32 的目录能被发现（试验场放在 tools/.scratch，跑完删掉）
_exp = os.path.join(os.path.dirname(os.path.abspath(__file__)), u".scratch", u"_挑解释器试验")
try:
    shutil.rmtree(_exp, ignore_errors=True)
    for _name in ("Python39", "Python312-32"):
        os.makedirs(os.path.join(_exp, _name))
        open(os.path.join(_exp, _name, "pythonw.exe"), "wb").close()
    _hit = launcher.scan_python_candidates([_exp])
    _names = sorted(os.path.basename(os.path.dirname(p)) for _k, p in _hit)
    check("扫描假目录树：Python39 和 Python312-32 都被发现",
          _names == ["Python312-32", "Python39"], "%s" % _names)
    check("扫描结果里 Python312-32 的排序键是 (12, 32)（会赢过 3.9）",
          max(k for k, _p in _hit) == (12, 32), "%r" % (max(k for k, _p in _hit),))
finally:
    shutil.rmtree(_exp, ignore_errors=True)

_own = launcher.beside_current_python()
check("能算出「启动自己的那个解释器」（依赖装在它里面，所以优先用它）",
      bool(_own) and os.path.isfile(_own), "%r" % (_own,))
check("它确实就是当前解释器同目录里的那个",
      os.path.dirname(os.path.abspath(_own)) == os.path.dirname(os.path.abspath(sys.executable)),
      "%r vs %r" % (_own, sys.executable))

# --------------------------------------------------------------------------- #
# 启动顺序：**先接管/起后端，再打无边框补丁**（2026-10-10 定，见项目文档更新记录 39）
#
# 为什么值得单列一条检查：补丁要等窗口句柄（`apply_borderless_when_ready` 里在轮询），
# 实测要 6–9 秒；而后端收到关闭信号后的宽限只有 15 秒（CLOSE_GRACE_SECONDS）。
# 顺序反过来的话，"关掉窗口后马上再双击"要等到宽限快到期时才去问"端口上那个后端
# 还能不能接管"——再叠上 shell 解析快捷方式的延迟就抢不到，只能自起一个新后端
# （能用，但慢几秒、也不是无缝接管）。补丁是装饰性的、后端是功能性的：功能性的先做。
# 这条盯着别被人"顺手"把顺序改回去。
# --------------------------------------------------------------------------- #
print()
print("=" * 70)
print(u"启动顺序：接管/起后端 必须 早于 无边框补丁")
print("=" * 70)
_launcher_src = open(os.path.join(V3, "launcher.py"), encoding="utf-8").read()
_w_start = _launcher_src.index("def worker(win):")
_w_body = _launcher_src[_w_start:_launcher_src.index('log("开始 pywebview 主循环")', _w_start)]
_i_probe = _w_body.find("reusable_backend(read_runtime_port()")
_i_patch = _w_body.find("if apply_borderless_when_ready(win):")
check(u"worker 里找得到「探测/接管后端」那一步", _i_probe >= 0, u"位置 %d" % _i_probe)
check(u"worker 里找得到「打无边框补丁」那一步", _i_patch >= 0, u"位置 %d" % _i_patch)
check(u"接管/起后端 排在 无边框补丁 之前（反了就会去抢 15 秒关闭宽限）",
      _i_probe >= 0 and _i_patch >= 0 and _i_probe < _i_patch,
      u"接管@%d vs 补丁@%d" % (_i_probe, _i_patch))

# --------------------------------------------------------------------------- #
# 窗口可见性兜底：ensure_window_visible()（2026-10-10 加，更新记录 39 后半段）
# 原始事故里"进程活着、日志一切正常、屏幕上什么都没有"的那一环：无边框补丁跑在
# pywebview「显示窗口」之前时，窗口停在"建好但没显示"（样式 0x06010000，没有
# WS_VISIBLE 位）。这条用一个**真的隐藏窗口**验兜底函数真的会把它显示出来。
# --------------------------------------------------------------------------- #
print()
print("=" * 70)
print(u"窗口可见性兜底：隐藏的窗口要能被补一次 SW_SHOW")
print("=" * 70)
_hwnd = None
try:
    launcher.setup_win32()
    _u32 = ctypes.windll.user32
    _u32.CreateWindowExW.restype = ctypes.c_void_p
    _u32.CreateWindowExW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                     ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _u32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _u32.IsWindowVisible.restype = ctypes.c_int
    _u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _u32.DestroyWindow.argtypes = [ctypes.c_void_p]
    # 样式传 0（不带 WS_VISIBLE）→ 建出来就是隐藏的，等同"补丁抢在显示之前"那个状态
    _hwnd = _u32.CreateWindowExW(0, u"STATIC", u"resume-tracker-visibility-probe",
                                 0, 0, 0, 100, 100, None, None, None, None)
    check(u"造得出一个隐藏窗口当试验品", bool(_hwnd), u"HWND=%r" % (_hwnd,))
    check(u"它一开始确实不可见（前提成立）",
          bool(_hwnd) and not _u32.IsWindowVisible(_hwnd))
    check(u"ensure_window_visible() 之后变可见（补丁抢了时序也救得回来）",
          bool(_hwnd) and bool(launcher.ensure_window_visible(_hwnd))
          and bool(_u32.IsWindowVisible(_hwnd)))
    check(u"对空句柄返回 False 且不抛异常（拿不到句柄也不能拖垮启动）",
          launcher.ensure_window_visible(None) is False)
finally:
    if _hwnd:
        try:
            ctypes.windll.user32.DestroyWindow(_hwnd)
        except Exception:
            pass

print("\n结果：" + ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
