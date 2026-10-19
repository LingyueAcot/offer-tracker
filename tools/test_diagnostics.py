# -*- coding: utf-8 -*-
r"""诊断报告 + 错误日志的回归测试（**不用开程序**，也不碰真实数据）。

    python tools\test_diagnostics.py

【为什么单独一个脚本】这是 2026-09-25 新增的"把别人电脑上的现场拿回来"的功能。
它有一个**必须靠自动检查守住**的性质：

    **报告里绝不能出现数据库内容**（投递记录 / 改动日志 / 个人画像全文），
    也不能出现开发者的这台电脑路径。否则"导出诊断报告"就成了一个数据泄露口子。

做法不是"我读了一遍觉得没有"，而是：**拿真库里的内容当探针，反过来查报告**
（只读打开 `app/data/applications.db`，取公司名/岗位名的片段当探针）。
这样"报告里混进了数据"这件事一定会被这条检查抓到。

【同时测的另外几件事】
- 环境采集不崩（非 Windows、注册表读不到都要能返回"未知"）
- 日志截断（取末尾 N 行、如实报告丢了多少行）
- 报告排版稳定（同样输入 → 同样输出，方便对比两份报告）
- 整份报告有字数上限（保险丝）
"""
from __future__ import print_function

import io
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
DATA = os.path.join(APP, "data")
sys.path.insert(0, APP)

from core.diagnostics import (MAX_REPORT_CHARS, TAIL_LINES,  # noqa: E402
                              build_report, contains_forbidden, format_env,
                              tail)
from services import diagnostics as diag  # noqa: E402

results = []


def check(name, ok, detail=u""):
    results.append(bool(ok))
    print(u"  [%s] %s%s" % (u"PASS" if ok else u"FAIL", name,
                            (u" -> " + detail) if detail else u""))


def db_probes(limit=200):
    """
    从真库取一些**只可能来自己用户数据**的字符串当探针（**只读**）。

    没有真库时返回空 —— 那不是失败，只是这条检查这次没东西可比。
    """
    path = os.path.join(DATA, "applications.db")
    if not os.path.isfile(path):
        return []
    out = []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
        for table, cols in (("applications", ("company", "position", "notes")),
                            ("talks", ("company", "summary", "notes"))):
            for col in cols:
                try:
                    rows = conn.execute(
                        "SELECT DISTINCT %s FROM %s WHERE %s != '' LIMIT ?"
                        % (col, table, col), (limit,)).fetchall()
                except Exception:
                    continue
                for (value,) in rows:
                    value = (value or u"").strip()
                    # 太短的串容易撞正常文本，取长一点的片段当探针
                    if len(value) >= 6:
                        out.append(value[:40])
        conn.close()
    except Exception:
        return []
    return out


# --------------------------------------------------------------------------- #
print(u"试验场：%s\n" % (u"（用系统临时目录）"))
scratch = tempfile.mkdtemp(prefix="diag_test_")
logs = os.path.join(scratch, "logs")
data = os.path.join(scratch, "data")
os.makedirs(logs)
os.makedirs(data)
diag.use(logs, data)

# ---------- 1) 环境采集 ----------
print(u"环境采集：")
env = diag.collect_env(u"1.0", u"personal", True,
                       {u"投递": 66, u"宣讲会": 130}, u"成功")
check(u"采集到程序版本与版别", u"1.0" in env.get("app", "") and u"个人版" in env.get("app", ""),
      env.get("app", ""))
check(u"采集到系统与 Python 版本",
      bool(env.get("os")) and bool(env.get("python")), env.get("os", ""))
# 屏幕缩放是排查「缩放相关」问题的关键（2026-09-24 那个最大化黑边只在非 100% 缩放出现）
check(u"采集到屏幕信息（含缩放，非 Windows 时允许为空）",
      isinstance(env.get("screen"), str))
check(u"数据量写进去了", u"投递 66" in env.get("data", ""), env.get("data", ""))
check(u"collect_env 对空参数不崩",
      bool(diag.collect_env()) is False or isinstance(diag.collect_env(), dict))
# 非 Windows / 读不到注册表时必须返回空而不是抛异常
check(u"WebView2 读不到时返回空字符串（不抛异常）",
      isinstance(env.get("webview2"), str))

# ---------- 2) 错误日志 ----------
print(u"\n错误日志：")
diag.log_error(u"测试用", u"Traceback (most recent call last):\nZeroDivisionError: x")
err_path = diag.error_log_path()
check(u"错误日志文件真的写出来了", os.path.isfile(err_path), err_path)
err_text = io.open(err_path, encoding="utf-8", errors="ignore").read()
check(u"里面有那条错误的原文", u"ZeroDivisionError" in err_text)
try:
    raise ValueError("故意抛一个")
except ValueError as exc:
    diag.log_exception(u"测试用-异常", exc)
err_text = io.open(err_path, encoding="utf-8", errors="ignore").read()
check(u"log_exception 记下了 traceback 和异常类型",
      u"ValueError" in err_text and u"故意抛一个" in err_text)

# ---------- 3) 截断 ----------
print(u"\n日志截断：")
long_text = "\n".join(u"第 %d 行" % i for i in range(1000))
got, dropped = tail(long_text, 100)
check(u"只取末尾 N 行", len(got.splitlines()) == 100, u"实际 %d 行" % len(got.splitlines()))
check(u"如实报告丢了多少行", dropped == 900, u"报的是 %s" % dropped)
check(u"短文本原样返回、不报丢弃", tail(u"a\nb", 100) == (u"a\nb", 0))
check(u"空文本不崩", tail(u"", 100) == (u"", 0))

# ---------- 4) 报告结构 ----------
print(u"\n报告结构：")
report = diag.build_diagnostic_report(env, u"1.1", u"personal")
for must in (u"应届海投管理 · 诊断报告", u"【运行环境】", u"【日志文件】",
             u"【错误日志】", u"【服务端日志", u"【启动器日志", u"ZeroDivisionError",
             # 窗口控件那一节是 2026-09-25 为「拖不动/不能最小化最大化」那份反馈加的
             u"【窗口控件"):
    check(u"报告里有「%s」" % must, must in report)

# 多屏 + 窗口现场（排查双屏/窗口跑到屏幕外这类问题要靠它们）
print(u"\n多屏与窗口现场：")
check(u"环境里带「虚拟桌面」范围（多屏时它是拼起来的整块）",
      u"虚拟桌面" in report)
check(u"环境里带「窗口状态」（上次的位置/大小/是否最大化）",
      u"窗口状态" in report)
# ⚠️ 「报告里有这个词」是不够的 —— 标签永远在，值可能一直是"读不到"。
#    这里真的**写一个窗口状态文件**，再断言它被解析出来了。
#    （第一版只查了标签，结果 `_window_state()` 因为漏了 `import json`
#      一直静默返回"读不到"，而检查照样 PASS —— 典型的"断言太弱等于没测"。）
import json as _json
with io.open(os.path.join(data, "window_state.json"), "w", encoding="utf-8") as handle:
    handle.write(_json.dumps({"maximized": True, "x": 324, "y": 190,
                              "w": 1368, "h": 699}))
window_detail = diag._window_state()
check(u"窗口状态真的被解析出来了（不是「读不到」）",
      u"324" in window_detail and u"1368" in window_detail
      and u"读不到" not in window_detail, window_detail)
env_probe = diag.collect_env(u"1.1", u"personal", True, {u"投递": 66}, u"成功")
check(u"环境里的「虚拟桌面」有值（多屏排查要用它）",
      bool(env_probe.get("virtual")), env_probe.get("virtual", u""))
# ⚠️ 别用 `bool(...)` 判"有没有值" —— "读不到" 也是非空字符串，`bool` 恒为 True。
#    突变验证时就是这么发现的：删掉 import json 之后这条照样 PASS。
#    要判的是"它到底读出来没有"，所以显式排除那几个表示失败的值。
_dead = (u"", u"读不到", u"还没有（第一次启动或没退出过）", u"内容不是对象", u"未知")
check(u"环境里的「窗口状态」真的有内容（不是「读不到」这类占位）",
      env_probe.get("window") not in _dead, env_probe.get("window", u""))

# ---------- 4b) 排版稳定 ----------
# ⚠️ 必须放在「往后写 error.log」**之前**：报告里含 error.log 的内容，
#    先写日志再比两次生成的结果，必然不同 —— 第一版就把它放在后面，误报了一次。
#    （这类"断言对了但时机错了"的坑，这个项目已经踩过好几次。）
print(u"\n排版稳定：")
again = diag.build_diagnostic_report(env, u"1.1", u"personal")
strip = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith(u"生成时间"))
check(u"同样输入 → 报告正文一致（去掉「生成时间」那一行后逐字相同）",
      strip(report) == strip(again))

# ---------- 5) ★ 核心：报告里绝不能有数据库内容 ----------
print(u"\n★ 报告不含数据库内容：")
probes = db_probes()
hits = contains_forbidden(report, probes)
check(u"报告里没有真库里的公司名/岗位名（探针 %d 个）" % len(probes),
      not hits, u"命中：%s" % u"、".join(hits[:3]) if hits else u"")
check(u"报告里没有改动日志的文件名",
      u"change_log.jsonl" not in report)
check(u"报告里没有人画像文件名",
      u"个人画像.json" not in report)
# 反向自检：探针机制真的能抓到东西（不然上面那条 PASS 毫无意义）
fake = report + u"\n" + (probes[0] if probes else u"某个不存在的公司名XYZ")
if probes:
    check(u"探针机制确实能抓到（故意塞一个探针进去 → 必须报命中）",
          bool(contains_forbidden(fake, probes)), u"塞的是：%s" % probes[0][:20])
else:
    check(u"探针机制确实能抓到（没有真库可比，跳过）", True, u"（无真库）")

# ---------- 6) 发布代码里没有写死「开发者这台机器」的路径 ----------
# ⚠️ 口径很重要，这一条我改过一次，两次都不对，记下来免得再错：
#    ① 第一版断言"报告里没有绝对路径" —— **错**。真实使用时报告里本来就有
#       **用户自己机器**的路径（`error.log` 里的 traceback 就长那样），那是正常现象。
#    ② 第二版断言"报告里没有开发者的项目路径" —— **也是错的**。自测时这个脚本
#       就在项目里，它故意抛的异常天然带着项目路径，必然命中。
#    真正要保证的是：**发出去的代码里不许写死开发者机器上的路径**（那才叫夹带）。
#    所以查的是「**打包清单里的那些文件**」，而不是整个 app/ ——
#    `开发记录` 这类内部文档本来就不进包（它的内容是这个项目的开发记录），
#    拿它当违规是误报。
# ⚠️ 打包清单**从 make_trial_package.py 现读**，不在这里重抄一份 ——
#    抄一份就会和真正的清单慢慢跑偏（这个项目吃过文档漂移的亏）。
print(u"\n发布代码里没有写死开发者机器路径：")
import ast as _ast
dev_path = os.path.abspath(ROOT)          # 开发者这台机器上的项目路径
dev_repo = os.path.dirname(dev_path)
needles = set([dev_path, dev_repo])

pkg_files, pkg_dirs = [], []
try:
    maker = io.open(os.path.join(ROOT, "tools", "make_trial_package.py"),
                    encoding="utf-8").read()
    for node in _ast.walk(_ast.parse(maker)):
        if isinstance(node, _ast.Assign):
            for target in node.targets:
                if not isinstance(target, _ast.Name):
                    continue
                if target.id == "FILES":
                    pkg_files = [v for v in _ast.literal_eval(node.value)]
                elif target.id == "DIRS":
                    pkg_dirs = [v for v in _ast.literal_eval(node.value)]
except Exception:
    pass

shipped = []
for name in pkg_files:
    shipped.append(os.path.join(APP, name))
for name in pkg_dirs:
    base = os.path.join(APP, name)
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith((".py", ".js", ".html")):
                shipped.append(os.path.join(dirpath, f))

hits = []
for path in shipped:
    if not os.path.isfile(path):
        continue
    try:
        text = io.open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    for needle in needles:
        if needle and needle in text:
            hits.append(u"%s: %s" % (os.path.relpath(path, APP), needle))
check(u"打包清单里的 %d 个文件都没有开发者机器的绝对路径" % len(shipped),
      not hits and len(shipped) > 10,
      u"；".join(hits[:3]) if hits else (u"清单抠到 %d 个文件" % len(shipped)))

# ---------- 7) 排版稳定 + 字数上限 ----------
print(u"\n排版与上限：")
huge = build_report(
    env=env, files=u"", error_log=u"",
    server_log=u"x\n" * 200000, launcher_log=u"y\n" * 200000,
    generated_at=u"2026-01-01 00:00:00", app_version=u"1.0", edition=u"personal")
check(u"超大输入时报告被截到上限以内", len(huge) <= MAX_REPORT_CHARS + 2000,
      u"实际 %d 字（上限 %d）" % (len(huge), MAX_REPORT_CHARS))
check(u"截断后头和尾都还在（环境信息 + 最近的日志）",
      u"【运行环境】" in huge and huge.rstrip().endswith(u"y"))

npass = sum(1 for r in results if r)
fail = len(results) - npass
print(u"\n共 %d 项：通过 %d · 失败 %d" % (len(results), npass, fail))
print(u"结果：%s" % (u"全部通过" if fail == 0 else u"存在失败项"))
sys.exit(0 if fail == 0 else 1)
