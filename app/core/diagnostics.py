# -*- coding: utf-8 -*-
"""
core/diagnostics.py —— 诊断报告的**纯规则**（2026-09-25 新增）

【这个文件解决什么问题】
用户在别人电脑上排查问题时，手里什么都没有 —— 因为服务端的输出被丢掉了
（见 `launcher.py` 的 `start_server`）。补上日志之后还需要一件事：
**把现场打成一个文件发给开发者**。

【为什么规则放 core】
报告要满足两条**能离线验证**的性质：
  1. **排版稳定**：同样输入给同样输出（不然每次导出的报告没法对比）；
  2. **绝不夹带数据库内容**：报告里永远不许出现投递记录、改动日志、个人画像全文。
第 2 条是这个功能唯一的真风险，所以它必须能被自动检查 —— 规则放 `core/`
就能在 `test_core.py` / `test_diagnostics.py` 里离线断言（不用开程序、不用真数据）。

【规矩】core 不许碰文件、不许 import 上层（AST 盯着）。
"""

# 报告里各部分最多带多少行。
# 为什么要有上限：日志会一直长，全发给开发者可能几 MB，微信都传不动；
# 而且**出问题的那一段一定在最后**（追加写），所以取末尾就够。
TAIL_LINES = 400

# 整份报告的字数上限（保险丝）。超了就继续从**中间**截，保留头（环境信息）和尾（最近的错误）。
MAX_REPORT_CHARS = 240000

# 报告抬头那句提醒 —— 用户 2026-09-25 定的口径：
# 「带对方的数据可以（他发给我＝他同意），但不能夹带我这边的」。
REPORT_BANNER = (
    "本文件由「应届海投管理」在你自己的电脑上生成，只包含这台机器的运行环境、"
    "最近的操作日志和程序报错，用于定位问题。\n"
    "它不会自动发送给任何人 —— 是你自己决定要不要发给开发者。\n"
    "⚠️ 日志里可能包含你操作过的公司名/岗位名，请只发给你信任的人。\n"
)


def _display_width(text):
    """
    算字符串在等宽字体里的**显示宽度**：中日韩字符占 2 格，其余占 1 格。

    为什么要这个：报告是给人**对齐着看**的，而 `len("程序：")` == 3、
    `len("Python：")` == 7，按字符数补齐会让中文标签那几行短一截。
    """
    width = 0
    for ch in str(text or ""):
        code = ord(ch)
        # 常见的 CJK / 全角区间（够用了，报告里的标签就那几种）
        if (0x1100 <= code <= 0x115F or 0x2E80 <= code <= 0xA4CF
                or 0xAC00 <= code <= 0xD7A3 or 0xF900 <= code <= 0xFAFF
                or 0xFE30 <= code <= 0xFE6F or 0xFF00 <= code <= 0xFF60
                or 0xFFE0 <= code <= 0xFFE6):
            width += 2
        else:
            width += 1
    return width


def format_env(env):
    """
    把环境信息字典排成一段人读的文本。

    缺的字段**如实写「未知」**，不省略、不留空 —— 排查时"这一项没采到"本身就是信息
    （例如注册表读不到 WebView2，往往说明系统上没装）。
    """
    rows = [
        ("程序", env.get("app")),
        ("系统", env.get("os")),
        ("Python", env.get("python")),
        ("屏幕", env.get("screen")),
        ("显示器", env.get("displays")),
        ("虚拟桌面", env.get("virtual")),
        ("窗口状态", env.get("window")),
        ("WebView2", env.get("webview2")),
        ("数据", env.get("data")),
        ("启动备份", env.get("backup")),
    ]
    # 标签列按**显示宽度**对齐到 24 格（"虚拟桌面：" 是最宽的那个：8 + 2 = 10 格 ×2）
    LABEL_WIDTH = 24
    lines = []
    for label, value in rows:
        text = str(value).strip() if value not in (None, "") else "未知"
        head = label + "："
        pad = max(1, LABEL_WIDTH - _display_width(head))
        lines.append("  " + head + " " * pad + text)
    return "\n".join(lines)


def tail(text, limit=TAIL_LINES):
    """
    取文本的**最后 limit 行**。空文本返回 `("", 0)` 形式由调用方处理。

    返回 `(截出来的文本, 被丢掉的原始行数)` —— 把丢了多少行写进报告，
    免得读的人以为"日志就这么短"。
    """
    if not text:
        return "", 0
    lines = text.splitlines()
    if len(lines) <= limit:
        return "\n".join(lines), 0
    return "\n".join(lines[-limit:]), len(lines) - limit


def section(title, body, note=u""):
    """报告里的一节（标题 + 可选说明 + 正文）。body 为空也要出标题，写明「（空）」。"""
    head = "\n" + "=" * 60 + "\n" + title + "\n" + "=" * 60
    if note:
        head += "\n" + note
    text = (body or "").strip()
    if not text:
        text = "（空）"
    return head + "\n" + text


def build_report(env, files, error_log, server_log, launcher_log, generated_at,
                 app_version=u"", edition=u"", shell_evidence=u""):
    """
    拼出完整报告。

    参数都是**纯数据**（字符串/字典），所以这个函数能离线测、能对着固定输入断言输出。

    顺序是刻意的：**先给结论性的东西（环境 + 窗口证据 + 错误），再给大段日志** ——
    开发者打开文件第一眼就能看到最可能的原因，不用翻 400 行。
    """
    parts = []
    parts.append("应届海投管理 · 诊断报告")
    parts.append("生成时间：%s" % (generated_at or "未知"))
    if app_version or edition:
        who = "个人版" if edition == "personal" else ("正式版" if edition == "release" else "")
        parts.append("版本：%s%s" % (
            (who + " ") if who else "", app_version or "未知"))
    parts.append("")
    parts.append(REPORT_BANNER)

    parts.append(section("【运行环境】", format_env(env)))
    # 「窗口控件」单独一节并放在**很前面**：它是"拖不动/不能最小化最大化"这类
    # 反馈的唯一直接证据，而且那类问题这台电脑往往复现不了（双屏、缩放不同都会影响）。
    if shell_evidence or shell_evidence == u"":
        parts.append(section(
            "【窗口控件（拖动 / 最小化 / 最大化）】", shell_evidence,
            "（这几行的意思是：窗口控件要靠「页面 → 启动器」一条这台电脑 HTTP 通道；"
            "日志里出现「页面已连上外壳桥」= 通道是通的）"))
    parts.append(section("【日志文件】", files or "", "（大小 / 行数 / 最后修改时间）"))
    parts.append(section("【错误日志】", error_log,
                         "（程序报错的原文。这一段通常就是定位问题的关键）"))
    err_tail, err_dropped = tail(server_log, TAIL_LINES)
    parts.append(section("【服务端日志 · 末尾 %d 行】" % TAIL_LINES, err_tail,
                         ("（前面还有 %d 行没放进来）" % err_dropped) if err_dropped else ""))
    launcher_tail, launcher_dropped = tail(launcher_log, TAIL_LINES)
    parts.append(section("【启动器日志 · 末尾 %d 行】" % TAIL_LINES, launcher_tail,
                         ("（前面还有 %d 行没放进来）" % launcher_dropped) if launcher_dropped else ""))

    text = "\n".join(parts) + "\n"

    # 保险丝：整份报告太大就从**中间**截（头和尾都要留住）
    if len(text) > MAX_REPORT_CHARS:
        keep = MAX_REPORT_CHARS // 2
        text = (text[:keep]
                + "\n\n……（中间省略 %d 字：报告超过 %d 字上限）……\n\n"
                % (len(text) - MAX_REPORT_CHARS, MAX_REPORT_CHARS)
                + text[-keep:])
    return text


def contains_forbidden(text, forbidden):
    """
    报告里**绝不允许**出现的东西（返回命中的那几条）。

    为什么做成函数而不是在测试里写死：这份名单是"红线"，
    应该和报告生成放在一起 review，而不是散在测试文件里。
    实际用的时候传的是：改动日志里的片段、数据库里抽出来的记录内容。
    """
    hits = []
    for item in (forbidden or []):
        item = str(item or "").strip()
        if item and item in text:
            hits.append(item)
    return hits
