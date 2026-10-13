# -*- coding: utf-8 -*-
"""
「桌面快捷方式」的回归测试（纯逻辑；最后一条会真建一次 .lnk，用假桌面）。

【为什么有它】
2026-09-27 用户反馈：**试用版解压出来双击 启动.cmd，桌面上什么都没有** ——
对方每次开机都得翻回那个文件夹。个人的那份是手工建的快捷方式，所以这个缺口一直没暴露。
修法是 launcher.py 的 `ensure_desktop_shortcut()`：启动时后台补一个。

这个脚本盯住四条「以后会被改回去」的约定：

  1. **只在缺失时建**（PowerShell 侧 `exit 4`）：用户可能自己改过图标/名字/目标，
     不能每次启动都给他改回去；
  2. **绝不指向控制台 python**：`python.exe` 会带一个黑窗口，而关掉那个黑窗口
     等于给整个进程组发关闭信号 —— 程序跟着退（2026-09-24 真踩过）；
  3. **中文名和中文路径不许直接写进命令行**：走 `-EncodedCommand`（UTF-16LE + Base64），
     否则会踩 cmd.exe / 控制台代码页的坑（`（开发期的一次性脚本）` 那次的办法）；
  4. **失败不许影响启动**：整条路包在一个 try 里、后台线程调用、日志只留一行。

最后一条会真的用 PowerShell 在一个**假桌面**（`RESUME_TRACKER_DESKTOP`）上建一次，
再把 .lnk 的每个字段读回来核对 —— 这是唯一能证明"字段真的写对了"的办法。
没有 PowerShell 时（非 Windows / 被裁掉）打 `[SKIP]`，不算失败。

用法：python tools\\test_shortcut.py
"""
import ast
import base64
import os
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True      # 别在 app\ 里留 __pycache__（下面要 import launcher）
sys.stdout.reconfigure(encoding="utf-8")

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)
import launcher  # noqa: E402

ok = True
skipped = []


def check(label, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("[PASS] " if cond else "[FAIL] ") + label + ("  " + extra if extra else ""))


def skip(label, why):
    skipped.append(label)
    print("[SKIP] " + label + "  " + why)


print("=" * 70)
print("一、该指向什么（shortcut_launch_command）")
print("=" * 70)
command = launcher.shortcut_launch_command()
check("算得出启动方式（返回 (target, arguments)）",
      isinstance(command, tuple) and len(command) == 2, "实际：%r" % (command,))

if isinstance(command, tuple) and len(command) == 2:
    target, arguments = command
    check("**不是**控制台 python.exe（黑窗口关掉会把程序带走）",
          not target.lower().endswith("python.exe"),
          "实际目标：%s" % os.path.basename(target))
    check("目标可执行文件真的存在", os.path.isfile(target), target)
    check("参数里带 launcher.py 的完整路径",
          "launcher.py" in arguments and os.path.join(launcher.BASE_DIR, "launcher.py") in arguments,
          "实际：%s" % arguments)
    if target.lower().endswith("wscript.exe"):
        check("走 wscript 兜底时，用的是包里的 _hidden_start.vbs（隐藏窗口）",
              "_hidden_start.vbs" in arguments, "实际：%s" % arguments)
    else:
        check("走 pythonw 时，参数只有 launcher.py 一个", arguments.count('"') == 2,
              "实际：%s" % arguments)

print()
print("=" * 70)
print("二、脚本内容（shortcut_script）—— 四条约定逐条查")
print("=" * 70)
script = launcher.shortcut_script(launcher.SHORTCUT_NAME, r"C:\x\pythonw.exe",
                                  r'"C:\x\launcher.py"', r"C:\x\web\app.ico")
check("① 已有就退出、不覆盖（PowerShell 侧 exit 4）", "exit 4" in script)
check("② 桌面位置问系统，不自己拼路径（GetFolderPath）", "GetFolderPath('Desktop')" in script)
check("③ 自检可以换一个假桌面（RESUME_TRACKER_DESKTOP）",
      "$env:RESUME_TRACKER_DESKTOP" in script)
check("   五个字段都写了（Target/Arguments/WorkingDirectory/Icon/Description）",
      all(k in script for k in ("$s.TargetPath=", "$s.Arguments=", "$s.WorkingDirectory=",
                                "$s.IconLocation=", "$s.Description=")))
check("   最后真的 Save()", "$s.Save()" in script)
check("   找不到桌面时用退出码 3 明确区分（不是静默成功）", "exit 3" in script)
no_icon = launcher.shortcut_script(launcher.SHORTCUT_NAME, "t", "a", "")
check("   没有图标文件时不写 IconLocation（留系统默认）", "$s.IconLocation=" not in no_icon)
check("快捷方式名字以 .lnk 结尾、描述非空",
      launcher.SHORTCUT_NAME.endswith(".lnk") and bool(launcher.SHORTCUT_DESC.strip()),
      "%s / %s" % (launcher.SHORTCUT_NAME, launcher.SHORTCUT_DESC))

print()
print("=" * 70)
print("三、静态：会不会又出「用了模块没 import」（2026-09-27 修过同类 bug）")
print("=" * 70)
source = open(os.path.join(APP, "launcher.py"), encoding="utf-8").read()
tree = ast.parse(source)
imported = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            imported.add((alias.asname or alias.name).split(".")[0])
    elif isinstance(node, ast.ImportFrom) and node.module:
        imported.add(node.module.split(".")[0])
used = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        used.add(node.value.id)
NEEDED = {"base64", "subprocess", "threading", "os", "sys", "json", "re", "socket",
          "time", "traceback", "ctypes"}
missing = sorted((used & NEEDED) - imported)
check("launcher.py 用到的模块全都 import 了", not missing, "缺：%s" % missing if missing else "")
check("新功能用的 base64 确实 import 了", "base64" in imported)

print()
print("=" * 70)
print("四、端到端：在**假桌面**上真建一次，再把字段读回来")
print("=" * 70)
powershell = launcher._powershell_exe()
if not os.path.isfile(powershell):
    skip("真建一次 .lnk 并读回字段", "这台机器上没有 PowerShell，跳过")
else:
    fake = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".scratch", "_快捷方式试验桌")
    shutil.rmtree(fake, ignore_errors=True)
    os.makedirs(fake, exist_ok=True)
    old_env = os.environ.get("RESUME_TRACKER_DESKTOP")
    os.environ["RESUME_TRACKER_DESKTOP"] = fake
    quiet = launcher.log
    launcher.log = lambda message: None          # 别往真 launcher.log 里灌测试记录
    try:
        made = launcher.ensure_desktop_shortcut()
        lnk = os.path.join(fake, launcher.SHORTCUT_NAME)
        check("第一次调用：假桌面上真的出现了这个 .lnk", made and os.path.isfile(lnk), lnk)

        read_ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
                   "Write-Output ('T=' + $s.TargetPath);"
                   "Write-Output ('A=' + $s.Arguments);"
                   "Write-Output ('W=' + $s.WorkingDirectory);"
                   "Write-Output ('I=' + $s.IconLocation)" % lnk)
        encoded = base64.b64encode(read_ps.encode("utf-16-le")).decode("ascii")
        got = subprocess.run([powershell, "-NoProfile", "-NonInteractive",
                              "-EncodedCommand", encoded],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
        fields = dict(line.split("=", 1) for line in got.stdout.splitlines() if "=" in line)
        check("   .lnk 的目标 = 我们算出来的那个可执行文件（不是控制台 python）",
              fields.get("T") == command[0], "实际：%r" % fields.get("T"))
        check("   .lnk 的工作目录 = 程序目录",
              os.path.normcase(fields.get("W") or "") == os.path.normcase(launcher.BASE_DIR),
              "实际：%r" % fields.get("W"))
        # 2026-10-11 改：原来只查"路径里有 app.ico"（松），现在**对着 paths 算出来的那份精确比**，
        # 并且顺带钉住那个真正重要的性质：**壳用那份不在程序目录里**
        # （程序目录这棵树可能带特殊 ACL/低完整性标签，Explorer 取图标会拒绝 —— 见 app/paths.py 的说明）。
        got_icon = (fields.get("I") or "").split(",")[0].strip('"')
        check("   .lnk 的图标 = 壳用那份（不在程序目录里）",
              os.path.normcase(got_icon) == os.path.normcase(launcher.SHELL_ICON)
              and not os.path.normcase(got_icon).startswith(os.path.normcase(launcher.BASE_DIR)),
              "实际：%r  期望：%r" % (fields.get("I"), launcher.SHELL_ICON))

        # 第二次：已有的**绝不许动**（改 mtime 也不行）
        stamp = os.path.getmtime(lnk)
        size = os.path.getsize(lnk)
        time.sleep(1.1)
        again = launcher.ensure_desktop_shortcut()
        check("第二次调用不进不改：mtime 和大小都没变",
              again and os.path.getmtime(lnk) == stamp and os.path.getsize(lnk) == size,
              "mtime 变了：%s" % (os.path.getmtime(lnk) != stamp))
    finally:
        launcher.log = quiet
        if old_env is None:
            os.environ.pop("RESUME_TRACKER_DESKTOP", None)
        else:
            os.environ["RESUME_TRACKER_DESKTOP"] = old_env
        shutil.rmtree(fake, ignore_errors=True)

# --------------------------------------------------------------------------- #
# 程序图标（2026-10-10 用户报"图标莫名其妙消失"，换成了自己给的图）
#
# 盯三件事，都是"以后会被改回去"的那类：
#   ① 图标文件在**程序内部**（`app\web\app.ico`）—— 不许指向下载目录那种外部路径，
#      源图被挪走/清掉就会"图标消失"；
#   ② `.ico` 里要装齐多种尺寸 —— 只塞一张大图，任务栏里缩到 16px 会糊成一团；
#   ③ 启动器**真的会在窗口上设它** —— 不设的话任务栏/Alt-Tab 退回 pythonw 的 Python 图标，
#      看起来同样像"图标没了"（这条是静态检查：`worker()` 里必须调 `apply_window_icon`）。
# --------------------------------------------------------------------------- #
print()
print("=" * 70)
print(u"程序图标：在程序内部、多尺寸齐全、运行时会设到窗口上")
print("=" * 70)


def ico_sizes(blob):
    """从 .ico 字节里读出「装了哪几个尺寸」（纯标准库，不依赖 Pillow）。"""
    if len(blob) < 6:
        return []
    count = int.from_bytes(blob[4:6], "little")
    out = []
    for i in range(count):
        off = 6 + i * 16
        if off + 2 > len(blob):
            break
        width = blob[off] or 256      # ICO 里 0 表示 256
        height = blob[off + 1] or 256
        out.append((width, height))
    return sorted(set(out))


check(u"launcher.APP_ICON 指的是程序目录里的 web\\app.ico（不是外部路径）",
      os.path.normcase(launcher.APP_ICON) == os.path.normcase(os.path.join(APP, "web", "app.ico")),
      launcher.APP_ICON)
_has_icon = os.path.isfile(launcher.APP_ICON)
check(u"图标文件真的在（换图后忘了生成就会红）", _has_icon,
      ("%d 字节" % os.path.getsize(launcher.APP_ICON)) if _has_icon else launcher.APP_ICON)
_sizes = ico_sizes(open(launcher.APP_ICON, "rb").read()) if _has_icon else []
check(u".ico 里装了至少 7 种尺寸（含 16x16 和 256x256）",
      len(_sizes) >= 7 and (16, 16) in _sizes and (256, 256) in _sizes,
      u"实际：%s" % u"、".join(u"%dx%d" % s for s in _sizes))


def ico_frames(blob):
    """读出每一帧的 (尺寸, 格式, 偏移)。格式判 BMP(DIB) / PNG —— 这条有讲究，见下面。"""
    import struct as _struct
    if len(blob) < 6:
        return []
    count = _struct.unpack_from("<H", blob, 4)[0]
    out = []
    for i in range(count):
        off = 6 + i * 16
        width = blob[off] or 256
        height = blob[off + 1] or 256
        size, data_off = _struct.unpack_from("<II", blob, off + 8)
        head = blob[data_off:data_off + 8]
        if head[:4] == b"\x89PNG":
            kind = "PNG"
        elif _struct.unpack_from("<I", head, 0)[0] == 40:
            kind = "BMP"
        else:
            kind = "?"
        out.append(((width, height), kind, data_off, size))
    return out


_frames = ico_frames(open(launcher.APP_ICON, "rb").read()) if _has_icon else []
_kinds = dict((s, k) for s, k, _o, _n in _frames)
# 2026-10-10：小尺寸帧必须是 BMP(DIB)、256 用 PNG —— Windows 的传统布局。
# 起初我全部用 PNG 帧，桌面/任务栏都不显示；换成 BMP 帧后资源管理器自己的图标 API
# （SHGetFileInfo）才肯给出图。这条盯着别改回去。
check(u"小尺寸帧是 BMP、256 帧是 PNG（Windows 传统布局）",
      _kinds.get((256, 256)) == "PNG" and all(_kinds.get(s) == "BMP" for s in _sizes if s[0] < 256),
      u"实际：%s" % u"、".join(u"%dx%d=%s" % (s[0], s[1], k) for s, k, _o, _n in _frames))


def bmp_frame_corner_alpha(blob, data_off, width, height):
    """从 BMP 帧里取四个角的 alpha（32 位 BGRA、自下而上）—— 背景抠没抠掉看这里。"""
    row = width * 4
    out = []
    for (x, y) in ((1, 1), (width - 2, 1), (1, height - 2), (width - 2, height - 2)):
        off = data_off + 40 + (height - 1 - y) * row + x * 4 + 3
        out.append(blob[off] if off < len(blob) else -1)
    return out


def bmp_frame_center_alpha(blob, data_off, width, height):
    off = data_off + 40 + (height - 1 - height // 2) * (width * 4) + (width // 2) * 4 + 3
    return blob[off] if off < len(blob) else -1


_bmp48 = [f for f in _frames if f[0] == (48, 48) and f[1] == "BMP"]
if _bmp48:
    _blob = open(launcher.APP_ICON, "rb").read()
    _o = _bmp48[0][2]
    _corners = bmp_frame_corner_alpha(_blob, _o, 48, 48)
    _center = bmp_frame_center_alpha(_blob, _o, 48, 48)
    check(u"背景是抠掉的：48x48 帧四角 alpha 都是 0（不透明背景会在这里露馅）",
          max(_corners) == 0, u"四角 alpha=%s" % _corners)
    check(u"主体还在：48x48 帧中心是实心的", _center > 200, u"中心 alpha=%s" % _center)
else:
    check(u"背景是抠掉的：48x48 帧四角 alpha 都是 0（不透明背景会在这里露馅）", False,
          u"没找到 48x48 的 BMP 帧")

# --------------------------------------------------------------------------- #
# 快捷方式的图标必须在「壳读得到」的位置（2026-10-10 查了一整轮才定位到）
#
# 实测（用 IShellItemImageFactory 看资源管理器真正会画的图）：程序目录那棵树如果带了
# 特殊 ACL / 低完整性标签（这台电脑 `<workspace>` 被沙箱打过标），**Explorer 取快捷方式图标时
# 会拒绝里面的图标文件** —— 快捷方式永远显示成一张通用图（10 色），而同一个文件原样
# 复制到用户目录下就正常（1000+ 色）。所以壳用的那份由 `sync_shell_icon()` 每次启动自愈同步。
# 顺带记两条被对照实验否掉的猜测：**引号**（给带空格的路径加引号反而取不到）和
# **空格/中文**（用户目录里 `my icons\app.ico`、`%USERPROFILE%\...` 都正常）。
# --------------------------------------------------------------------------- #
_app_norm = os.path.normcase(os.path.abspath(APP))
check(u"壳用的图标不在程序目录里（那棵树可能被打了沙箱标签，Explorer 读不了）",
      not os.path.normcase(os.path.abspath(launcher.SHELL_ICON)).startswith(_app_norm),
      launcher.SHELL_ICON)
_shell_icon = launcher.sync_shell_icon()
_ok_sync = (bool(_shell_icon) and os.path.isfile(_shell_icon)
            and open(_shell_icon, "rb").read() == open(launcher.APP_ICON, "rb").read())
check(u"sync_shell_icon() 真的同步出来了，而且与程序内部那份逐字节一致", _ok_sync,
      u"%s（%d 字节）" % (_shell_icon, os.path.getsize(_shell_icon) if os.path.isfile(_shell_icon) else 0))
_script = launcher.shortcut_script("x.lnk", "t.exe", "arg", launcher.SHELL_ICON,
                                   legacy_icon=launcher.APP_ICON)
check(u"脚本里图标写的是壳那份、而且**没加引号**（加引号 Explorer 会取不到）",
      (u"'%s,0'" % launcher.SHELL_ICON) in _script
      and (u'"%s' % launcher.SHELL_ICON) not in _script,
      u"出现引号=%s" % ((u'"%s' % launcher.SHELL_ICON) in _script))
check(u"脚本认得「我们自己写坏的那种图标」（老快捷方式才修得回来）",
      (u"'%s'" % launcher.APP_ICON) in _script, u"legacy=%s" % launcher.APP_ICON)
_src = open(os.path.join(APP, "launcher.py"), encoding="utf-8").read()
_worker = _src[_src.index("def worker(win):"):_src.index('log("开始 pywebview 主循环")')]
check(u"worker() 里真的调了 apply_window_icon（不设就退回 pythonw 的图标）",
      "apply_window_icon(" in _worker,
      u"worker 段内 %d 处" % _worker.count("apply_window_icon("))

print()
if skipped:
    print("跳过 %d 项：%s" % (len(skipped), "、".join(skipped)))
print("结果：" + ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
