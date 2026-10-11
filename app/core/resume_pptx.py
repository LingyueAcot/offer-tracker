# -*- coding: utf-8 -*-
"""
core/resume_pptx.py —— 把简历正文 / 排版算成「每页有哪些文字行、放在哪儿」（纯函数）

【为什么单独一个文件】
导出 PPTX 分两半：**算布局**（这里）和**生成 OOXML 字节**（`services/resume_pptx.py`）。
算布局是纯算术，不碰文件、不碰 zip、不碰数据库，所以能用普通的字典测 —— 分页边界、
左右栏归属、隐藏板块、自定义标题这些规则都能离线钉死。

【规矩】同 core/ 其他模块：不许 import server / sqlite3 / urllib / os / io / json / pathlib，
不许 open() 文件。这里的输出就是普通 dict / 数字。

【坐标系】毫米、原点在页面左上角。与用户 PPTX 原件的几何一致：
A4 210×297、抬头 47.1、左栏 x=12.2 宽 58.0、右栏 x=78.3 宽 119.6、底部留白 8。
"""
import math

from core.values import clean

PAGE_W_MM = 210.0
PAGE_H_MM = 297.0
BAND_H_MM = 47.1
LEFT_X_MM = 12.2
# 左栏文字可用宽：58.0 → **61.0**（2026-10-11 用户选 B：分栏线不动、只把文字放宽）。
# 原因：荣誉奖项里「中国人民解放军“四有”优秀军人（2024）」这类 19 个单位的长条目，
# 在 58mm 下必然折行、把「（2024）」挤成单独一行（外部评审 连续两轮判为必改）。
LEFT_W_MM = 61.0
RIGHT_X_MM = 78.3
RIGHT_W_MM = 119.6
BOTTOM_MM = 8.0

# 抬头内部坐标（与 web 预览、PPTX 原件三处一致）
NAME_Y_MM = 6.4
SUB_Y_MM = 17.5
INFO_Y_MM = (25.9, 32.3, 38.6)

# 正文栏的起始高度：首页接着抬头往下，续页从顶部留一点写「第 N 页」
COLUMN_TOP_MM = BAND_H_MM + 3.0
CONT_TOP_MM = 16.0
CONT_HEAD_Y_MM = 8.0

MM_PER_PT = 25.4 / 72.0

# 字号（pt）——取自己用户 PPTX 原件实测值，再乘用户设定的 font_scale
SIZE_NAME = 24.0
SIZE_SUB = 10.15
SIZE_INFO = 9.4
SIZE_SECTION = 11.65
SIZE_ORG = 10.5
SIZE_BODY = 9.0
SIZE_RULE = 0.35          # 板块标题下的分隔线粗细（mm）
# 荣誉奖项这一节单独小一号（2026-10-11）：像「中国人民解放军“四有”优秀军人（2024）」
# 这种 19 个单位的长条目，在 61mm 宽、9.45pt 下**数学上放不进一行**（最多 18.3 个中文字），
# 必然把「（2024）」挤成孤立一行。缩到 8.6pt 后 21 个字都能放下（外部评审 连续三轮判为必改）。
SIZE_AWARDS = 8.6

MIN_ITEM_MM = 4.0         # 一个条目至少占这么高（防止空条目叠在一起）
SAFETY = 1.02             # 尺寸预算的安全系数（字体度量与实际渲染有偏差，宁可保守）
LINE_MIN = 1.25           # 行距下限：再小会切到字（导出时按精确行距写死，太紧会裁字）

# 段间距（mm）：**故意做得比"段内行距"只大一点点** ——
# 2026-10-11 用户报「段落与段落间隔和段落内行间隔相差太大，段落分割太明显」。
# 原来每个文本框内部用 PowerPoint 默认行距（≈1.2×字号），而段间按 1.55× 分配，
# 两者差近一半，看起来就像"每条之间都空一行"。现在段内用精确行距（与分配一致），
# 段间只多出下面这两个小值：行与行 0.35mm、条目与条目 +1.0mm。
GAP_LINE_MM = 0.35
GAP_ITEM_MM = 1.0
# 板块之间的间距**上限**（mm）：铺满时不要把剩余空间全都塞进模块之间 ——
# 用户 2026-10-11 报「只是简单均分，没有考虑美观」：全塞进去会出现几厘米的大空档。
# 超过这个上限的部分留在页面底部（宁愿底部留白，也不要模块之间忽大忽小）。
MAX_GAP_MM = 5.0
RULE_BAND_MM = 2.55       # 板块标题与正文之间留给那条细线的带宽
HEADER_LINE = 1.1         # 抬头那几行用**紧行距**：24pt 名字 ×1.55 会高到压住副标题
                          #（2026-10-11 用户报「顶部重叠了」就是这个原因）


def effective_line_height(line_height):
    """实际用于排版的行程倍数：不低于 LINE_MIN（否则精确行距会切字）。"""
    try:
        value = float(line_height)
    except (TypeError, ValueError):
        return 1.55
    return max(LINE_MIN, value)

DEFAULT_TITLES = {
    "education": "教育经历", "work": "实习 / 工作", "military": "部队经历",
    "campus": "在校经历", "projects": "项目经历", "skills": "专业技能", "awards": "荣誉奖项",
}

DEFAULT_THEME = {"header_bg": "#3B5F82", "header_text": "#FFFFFF", "accent": "#2F6595"}
TEXT_COLOR = "1F2937"
DATE_COLOR = "3A4048"

# 三条**淡色**装饰线（取自己用户 PPTX 原件的实测值）。
# ⚠️ 别拿 accent（深蓝）去画它们：2026-10-11 我用 accent 画了板块标题下的线，
# 用户一眼看出「原来是淡颜色，怎么变成实心颜色了」。
RULE_COLOR = "D5E1EC"        # 板块标题下的细线（原件 edu-rule）
DIVIDER_COLOR = "E2EAF2"     # 左右两栏之间的竖线（原件 column-divider）
BAND_RULE_COLOR = "B4C9DB"   # 抬头色块下沿的细线（原件 band-rule）
DIVIDER_X_MM = 74.3          # 分栏线位置（原件 x=7.43cm）

# ── 证件照框（用户原件实测几何）与抬头右边界的规则（2026-10-11 用户拍板） ──
# 用户原话：「没有照片时，文字位置可以放宽」+「按你推荐的」⇒
#   有照片：文字右边界让到照片框左侧 − 间隙；没有照片/隐藏：放宽到页面右边距。
PHOTO_X_MM = 167.7
PHOTO_Y_MM = 4.8
PHOTO_W_MM = 30.7
PHOTO_H_MM = 40.2
PHOTO_GAP_MM = 3.0                     # 文字与照片之间的最小间隙
TEXT_RIGHT_FULL_MM = PAGE_W_MM - 12.1  # 没有照片时的右边界（197.9）
INFO_SIZE_MIN = 8.5                    # 信息行字号下限（再小就不体面了）
INFO_SIZE_STEP = 0.1                   # 每次缩 0.1pt
CONT_GAP_MM = 3.0                      # 续排区与下方两栏之间的间隙
# ⚠️ 打包时的安全余量（2026-10-11 外部评审 看图抓到折行后加的）：
# 我的宽度是"字符估宽"（中文 1、西文 0.55 个字号宽），真实字体往往略宽。
# 按 100% 可用宽度排会贴边 —— 实测有一行估 177.5mm（可用 185.7mm）看起来很安全，
# 但真实渲染宽了 5% 就被渲染器自己折行，折出的第二行溢到下一个固定 y、和下一行挤在一起。
# 所以只按可用宽度的 93% 排，留出余量。
PACK_SAFETY = 0.93
LATIN_UNITS = 0.55                     # 西文/数字的估算单位（原来 0.5 偏乐观）


def has_photo(content, layout):
    """这份简历当前**要显示**照片吗（有照片数据 且 show_photo 为真）。"""
    return bool((content or {}).get("photo")) and bool((layout or {}).get("show_photo"))


def header_text_width_mm(show_photo):
    """抬头文字的可用宽度：有照片时避让照片框，没有照片时用整条（用户拍板）。"""
    right = (PHOTO_X_MM - PHOTO_GAP_MM) if show_photo else TEXT_RIGHT_FULL_MM
    return max(60.0, right - LEFT_X_MM)


def text_units(text):
    """粗略的「显示宽度」：中文算 1、西文/数字算 0.55（偏保守，宁可多留余量）。"""
    total = 0.0
    for char in text or "":
        total += 1.0 if ord(char) > 0x2E80 else LATIN_UNITS
    return total


def wrapped_lines(text, width_mm, size_pt):
    """这段文字在给定宽度、字号下大概会折成几行（至少 1 行）。"""
    if not text:
        return 1
    unit_mm = size_pt * MM_PER_PT
    if unit_mm <= 0 or width_mm <= 0:
        return 1
    per_line = max(1.0, width_mm / unit_mm)
    return max(1, int(math.ceil(text_units(text) / per_line - 1e-9)))


def _fit_info(pairs, width_mm, scale, max_rows):
    """在「3 行以内 + 字号不低于 8.5pt」的前提下，取**最大字号**能装下的方案。

    返回 (放得下的行, 字号, 装不下的条目)。一条都装不下时返回 ([], 最小字号, 全部条目)。
    """
    size = SIZE_INFO * scale
    while size >= INFO_SIZE_MIN * scale - 1e-6:
        rows = _pack_info_all(pairs, width_mm, size)
        if len(rows) <= max_rows:
            return rows, round(size, 2), []
        size = round(size - INFO_SIZE_STEP * scale, 2)
    floor = INFO_SIZE_MIN * scale
    rows = _pack_info_all(pairs, width_mm, floor)
    if len(rows) <= max_rows:
        return rows, round(floor, 2), []
    return rows[:max_rows], round(floor, 2), _pairs_after_rows(pairs, rows[:max_rows], floor, width_mm)


def _pack_info_all(pairs, width_mm, size_pt):
    """按宽度把条目全部打包成行（不做行数限制）。

    ⚠️ 只按 `width_mm × PACK_SAFETY` 排（安全余量，见常量注释）——
    贴边排的话渲染器会自己折行，折出的第二行溢到下一个固定 y 上。

    ⚠️ **单条比一行还宽时必须先切开**（2026-10-11 外部评审 看图抓到的碰撞）：
    否则打包会把它整条当成一行，同样会被渲染器折行。
    """
    limit = width_mm * PACK_SAFETY
    expanded = []
    for pair in pairs:
        expanded.extend(_split_wide_pair(pair, limit, size_pt))
    rows = []
    current = []
    base = size_pt * MM_PER_PT
    used = 0.0
    for pair in expanded:
        pair_width = text_units(pair) * base
        add = pair_width if not current else base + pair_width
        if current and used + add > limit:
            rows.append("　".join(current))
            current = [pair]
            used = pair_width
        else:
            current.append(pair)
            used += add
    if current:
        rows.append("　".join(current))
    return rows


def _split_wide_pair(pair, width_mm, size_pt):
    """把一条过宽的信息按估算宽度切成几段（中文可以在任意字之间断）。"""
    unit = size_pt * MM_PER_PT
    if unit <= 0:
        return [pair]
    limit = max(1.0, width_mm / unit)
    if text_units(pair) <= limit:
        return [pair]
    chunks = []
    current = ""
    used = 0.0
    for char in pair:
        char_units = 1.0 if ord(char) > 0x2E80 else 0.5
        if current and used + char_units > limit:
            chunks.append(current)
            current = char
            used = char_units
        else:
            current += char
            used += char_units
    if current:
        chunks.append(current)
    return chunks


def _pairs_after_rows(pairs, keep_rows, size_pt, width_mm):
    """算出「前 keep_rows 行之后还剩哪些条目」—— 用于正文顶部续排（不丢信息）。"""
    rows = _pack_info_all(pairs, width_mm, size_pt)
    kept = sum(len(row.split("　")) for row in keep_rows)
    return pairs[kept:] if kept < len(pairs) else []


def row_height_mm(text, width_mm, size_pt, line_height, gap_mm):
    """一行文字占的高度：折行数 × 字号 × 行距 + 行后间距，再乘安全系数。

    ⚠️ 这里的行距必须与导出时写进 XML 的**精确行距**用同一个值
    （`effective_line_height`），否则分配高度与实际渲染对不上，
    表现就是段间比段内空得多（2026-10-11 踩过）。
    """
    lines = wrapped_lines(text, width_mm, size_pt)
    return (lines * size_pt * MM_PER_PT * effective_line_height(line_height) + gap_mm) * SAFETY


def _theme(layout):
    theme = dict(DEFAULT_THEME)
    incoming = (layout or {}).get("theme") or {}
    for key in DEFAULT_THEME:
        value = clean(incoming.get(key), 7)
        if value and len(value) == 7 and value.startswith("#"):
            theme[key] = value.upper()
    return theme


def _sections(content, layout):
    """
    按左右栏归属，把要输出的板块整理成 [(side, key, title, items, kind)]（顺序即输出顺序）。

    `kind`："text" = 每条一段文本（技能/荣誉，以及**自定义文本板块**）；"complex" = 有单位/角色/起止/要点。
    ⚠️ 不再用"键是不是 skills/awards"来判断（2026-10-11）：自定义板块的键是 cs_1 这种，
       条目结构却和技能/荣誉一样，靠键名判断必然漏。
    """
    layout = layout or {}
    order = layout.get("section_order") or {}
    titles = layout.get("section_titles") or {}
    hidden = set(layout.get("hidden_sections") or [])
    custom = {}
    for entry in (content or {}).get("custom_sections") or []:
        if not isinstance(entry, dict) or not entry.get("id") or entry.get("deleted"):
            continue
        custom[str(entry["id"])] = list(entry.get("items") or [])
    known = set(DEFAULT_TITLES) | set(custom)
    blocks = []
    placed = set()
    for side, default_keys in (("left", ("education", "skills", "awards")),
                              ("right", ("military", "projects", "work", "campus"))):
        keys = [k for k in (order.get(side) or list(default_keys)) if k in known]
        for key in keys:
            if key in hidden:
                continue
            items = custom[key] if key in custom else ((content or {}).get(key) or [])
            if not items:
                continue
            placed.add(key)
            title = clean(titles.get(key), 20) or DEFAULT_TITLES.get(key) or "自定义板块"
            kind = "text" if (key in ("skills", "awards") or key in custom) else "complex"
            blocks.append((side, key, title, items, kind))
    # 新建的自定义板块还没被排进任何一栏时 → 追加到右栏末尾（不能凭空消失）
    for key, items in custom.items():
        if key in placed or key in hidden or not items:
            continue
        blocks.append(("right", key, clean(titles.get(key), 20) or "自定义板块", items, "text"))
    return blocks


def _item_rows(key, item, x_mm, width_mm, scale, line_height, kind=None):
    """一个条目 → 若干行（公司/角色 + 时间、小节、描述要点）。"""
    rows = []
    if not isinstance(item, dict):
        return rows
    date = clean(item.get("start"), 32)
    end = clean(item.get("end"), 32)
    if date and end:
        date = date + " – " + end
    elif end:
        date = end

    def add(text, size_pt, bold=False, color=TEXT_COLOR, gap=GAP_LINE_MM):
        text = clean(text, 2000)
        if not text:
            return
        rows.append({
            "text": text, "x_mm": x_mm, "width_mm": width_mm,
            "size_pt": round(size_pt * scale, 2), "bold": bold, "color": color,
            "space_after_mm": 0.0,
            "height_mm": round(row_height_mm(text, width_mm, size_pt * scale, line_height, gap), 2),
        })

    if key == "projects":
        head = clean(item.get("name"), 200)
        if item.get("role"):
            head = (head + "　" + clean(item.get("role"), 200)).strip()
        add(head, SIZE_ORG, bold=True)
        if item.get("topic"):
            add("课题：" + clean(item.get("topic"), 500), SIZE_BODY, color=DATE_COLOR)
        for sub in item.get("sections") or []:
            if not isinstance(sub, dict):
                continue
            label = clean(sub.get("label"), 60)
            text = clean(sub.get("text"), 2000)
            if label and text:
                add(label + "：" + text, SIZE_BODY)
            elif text:
                add(text, SIZE_BODY)
        for bullet in item.get("bullets") or []:
            add("• " + clean(bullet, 2000), SIZE_BODY)
    elif key == "education":
        add(clean(item.get("school"), 200), SIZE_ORG, bold=True)
        add("　".join([clean(item.get("major"), 200), clean(item.get("degree"), 100)]).strip(),
            SIZE_BODY, color=DATE_COLOR)
        for bullet in item.get("bullets") or []:
            add("• " + clean(bullet, 2000), SIZE_BODY)
    elif kind == "text" or key in ("skills", "awards"):
        # 文本型：技能/荣誉 + 自定义文本板块（荣誉保持它自己那号，其余用正文号）
        add(clean(item.get("text"), 2000), SIZE_AWARDS if key == "awards" else SIZE_BODY)
    else:
        head = clean(item.get("org"), 200)
        if item.get("role"):
            head = (head + "　" + clean(item.get("role"), 200)).strip()
        add(head, SIZE_ORG, bold=True)
        for bullet in item.get("bullets") or []:
            add("• " + clean(bullet, 2000), SIZE_BODY)

    if date:
        rows.insert(0, {
            "text": date, "x_mm": x_mm, "width_mm": width_mm,
            "size_pt": round(SIZE_BODY * scale, 2), "bold": False, "color": DATE_COLOR,
            "space_after_mm": 0.0,
            "height_mm": round(row_height_mm(date, width_mm, SIZE_BODY * scale, line_height, 0.4), 2),
        })
    height = sum(row["height_mm"] for row in rows)
    if height < MIN_ITEM_MM:
        rows[-1]["height_mm"] = round(rows[-1]["height_mm"] + (MIN_ITEM_MM - height), 2)
    if rows:
        # 条目的最后一行多留一点：让"条目之间"只比"条目内行距"略大。
        # 注意这是**段落间距**（写进 XML 的 spcAft），不是靠"每行一个文本框 + 估算高度"堆出来的 ——
        # 2026-10-11 之前就是那么做的，估多了出现大空隙（用户报「间隔太远」）、估少了压字串行。
        rows[-1]["space_after_mm"] = GAP_ITEM_MM
        rows[-1]["height_mm"] = round(rows[-1]["height_mm"] + GAP_ITEM_MM, 2)
    return rows


def _block_rows(side, key, title, items, layout, scale, line_height, kind=None):
    """一个板块 → 若干行（标题 + 分隔线 + 各条目）。"""
    x_mm = LEFT_X_MM if side == "left" else RIGHT_X_MM
    width_mm = LEFT_W_MM if side == "left" else RIGHT_W_MM
    rows = [{
        "text": title, "x_mm": x_mm, "width_mm": width_mm, "title": True,
        "size_pt": round(SIZE_SECTION * scale, 2), "bold": True, "color": "accent",
        "height_mm": round(row_height_mm(title, width_mm, SIZE_SECTION * scale, 1.2, 0.8), 2),
    }, {
        "text": "", "x_mm": x_mm, "width_mm": width_mm, "rule": True,
        "size_pt": 0.0, "bold": False, "color": "rule",
        "height_mm": round(SIZE_RULE + 2.2, 2),
    }]
    for item in items:
        rows.extend(_item_rows(key, item, x_mm, width_mm, scale, line_height, kind))
    if rows:
        # 板块的最后一行不留段落间距（它后面没有同框的下一段了，留了就是一段白）
        rows[-1]["space_after_mm"] = 0.0
    return rows


def plan(content, layout):
    """
    算出版式计划：{"pages": [{"rows": [...]}, ...], "theme": {...}}

    - 首页顶部是抬头色块 + 姓名 / 副标题 / 三行信息（坐标固定）；
    - 正文按左右栏各自往下排；**条目不会被拆到两页**（放不下就整条挪到下一页）；
    - 一页装不下就续页，续页顶部写一行「姓名 · 简历（第 N 页）」，栏位从 CONT_TOP_MM 开始。
    """
    layout = layout or {}
    theme = _theme(layout)
    scale = layout.get("font_scale") or 1.0
    line_height = layout.get("line_height") or 1.55
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        scale = 1.0
    if scale < 0.5 or scale > 2.0:
        scale = 1.0
    try:
        line_height = float(line_height)
    except (TypeError, ValueError):
        line_height = 1.55
    if line_height < 1.0 or line_height > 2.5:
        line_height = 1.55

    basics = (content or {}).get("basics") or {}
    header_rows = []
    if clean(basics.get("name"), 60):
        header_rows.append({"text": clean(basics.get("name"), 60), "x_mm": LEFT_X_MM,
                            "width_mm": PAGE_W_MM - LEFT_X_MM * 2, "size_pt": round(SIZE_NAME * scale, 2),
                            "bold": True, "color": "header_text", "y_mm": NAME_Y_MM, "height_mm": 12.0,
                            "fixed": True, "line_height": HEADER_LINE})
    header_rows.append({"text": clean(basics.get("headline"), 200), "x_mm": LEFT_X_MM,
                        "width_mm": PAGE_W_MM - LEFT_X_MM * 2, "size_pt": round(SIZE_SUB * scale, 2),
                        "bold": False, "color": "header_text", "y_mm": SUB_Y_MM, "height_mm": 5.0,
                        "fixed": True, "line_height": HEADER_LINE})

    # 基本信息行：来自 content["info"]（最多 8 条、标签内容都可自定义），
    # 抬头里每行最多并排 3 条（共 3 行 → 正好容下 8 条）。
    # 老数据没有 info 键时回落到 basics 的 8 个既定字段（迁移规则与 core/resume.py 一致）。
    pairs = []
    raw_info = (content or {}).get("info")
    if isinstance(raw_info, list) and raw_info:
        for entry in raw_info:
            if not isinstance(entry, dict):
                continue
            label = clean(entry.get("label"), 20)
            value = clean(entry.get("value"), 80)
            if value:
                pairs.append((label + "：" if label else "") + value)
    else:
        for label, field in (("电话", "phone"), ("邮箱", "email"), ("性别", "gender"),
                             ("现所在地", "location"), ("政治面貌", "political"),
                             ("民族", "ethnicity"), ("籍贯", "hometown"),
                             ("最高学历", "education_level")):
            value = clean(basics.get(field), 80)
            if value:
                pairs.append(label + "：" + value)

    # 抬头文字宽度：**有照片时避让照片框**（用户 2026-10-11 拍板：没有照片就放宽到整条色块）
    info_width = header_text_width_mm(has_photo(content, layout))
    info, info_size, info_rest = _fit_info(pairs, info_width, scale, len(INFO_Y_MM))
    while len(info) < len(INFO_Y_MM):
        info.append("")
    for index, text in enumerate(info):
        header_rows.append({"text": text, "x_mm": LEFT_X_MM, "width_mm": info_width,
                            "size_pt": info_size, "bold": False,
                            "color": "header_text", "y_mm": INFO_Y_MM[index], "height_mm": 4.2,
                            "fixed": True, "line_height": HEADER_LINE})

    # 抬头里装不下的信息行 → **正文顶部续排**（一条都不许丢；色块不加高、不加第 4 行）
    cont_rows = []
    cont_height = 0.0
    if info_rest:
        cont_width = PAGE_W_MM - LEFT_X_MM * 2
        cont_size = round(SIZE_INFO * scale, 2)
        for text in _pack_info_all(info_rest, cont_width, cont_size):
            height = round(row_height_mm(text, cont_width, cont_size, line_height, 0.4), 2)
            cont_rows.append({"text": text, "x_mm": LEFT_X_MM, "width_mm": cont_width,
                              "size_pt": cont_size, "bold": False, "color": TEXT_COLOR,
                              "height_mm": height, "fixed": True, "line_height": HEADER_LINE})
            cont_height += height

    blocks = {side: [] for side in ("left", "right")}
    for side, key, title, items, kind in _sections(content, layout):
        blocks[side].append((key, _block_rows(side, key, title, items, layout, scale, line_height, kind)))

    pages = []
    pending = {side: list(blocks[side]) for side in ("left", "right")}
    first = True
    while any(pending.values()):
        rows = (list(header_rows) + list(cont_rows)) if first else []
        if not first:
            name = clean(basics.get("name"), 60) or "简历"
            rows.append({"text": name + " · 简历（第 %d 页）" % (len(pages) + 1),
                         "x_mm": LEFT_X_MM, "width_mm": PAGE_W_MM - LEFT_X_MM * 2,
                         "size_pt": round(SIZE_INFO * scale, 2), "bold": False,
                         "color": "date", "y_mm": CONT_HEAD_Y_MM, "height_mm": 4.2,
                         "fixed": True, "line_height": HEADER_LINE})
        # 有续排信息行时，两栏整体往下让出续排区的高度
        top = COLUMN_TOP_MM if first else CONT_TOP_MM
        if first and cont_rows:
            top = COLUMN_TOP_MM + cont_height + CONT_GAP_MM
        bottom = PAGE_H_MM - BOTTOM_MM
        placed_ranges = {"left": [], "right": []}
        for side in ("left", "right"):
            y = top
            while pending[side]:
                key, block_rows = pending[side][0]
                height = sum(row["height_mm"] for row in block_rows)
                if y + height > bottom and y > top:
                    break                      # 这一栏放不下了，留给下一页
                start = len(rows)
                for row in block_rows:
                    placed = dict(row)
                    placed["y_mm"] = y
                    placed["side"] = side
                    rows.append(placed)
                    y += row["height_mm"]
                placed_ranges[side].append((start, len(rows) - start))
                pending[side].pop(0)

        # 铺满：把每栏剩下的空间**均分到板块之间**（与网页预览的弹性间隔同一套做法）。
        # ⚠️ 只有「这一栏的板块全部排完了」才能均分 —— 2026-10-11 的 bug：
        # 荣誉奖项装不下被推到第 2 页，左栏只剩 2 个板块、底部空 103mm，
        # 均分就把这 103mm 塞进了两个板块中间，渲染出来中间裂开一个 10 厘米的大洞。
        # 只要该栏还有板块留给下一页，就必须老老实实从上往下排。
        for side in ("left", "right"):
            entries = placed_ranges[side]
            if len(entries) < 2 or pending[side]:
                continue
            last_start, last_count = entries[-1]
            last_row = rows[last_start + last_count - 1]
            used_bottom = last_row["y_mm"] + last_row["height_mm"]
            # 铺满只是"补一点"，不是"把剩余空间全塞进来"（上限 MAX_GAP_MM）
            extra = min((bottom - used_bottom) / (len(entries) - 1), MAX_GAP_MM)
            if extra <= 0.1:
                continue
            for step in range(1, len(entries)):
                start, count = entries[step]
                for index in range(start, start + count):
                    rows[index]["y_mm"] = round(rows[index]["y_mm"] + extra * step, 2)

        # 页面装饰：抬头下沿的细线（只首页）+ 左右两栏之间的竖线（每页）。
        # 放在「铺满均分」之后追加，所以它们不会被均分逻辑挪动。
        if first:
            rows.append({"kind": "band_rule", "text": "", "x_mm": 0.0, "y_mm": BAND_H_MM,
                         "width_mm": PAGE_W_MM, "height_mm": 0.8, "color": "band_rule"})
        rows.append({"kind": "divider", "text": "", "x_mm": DIVIDER_X_MM, "y_mm": top,
                     "width_mm": 0.3, "height_mm": round(bottom - top, 2), "color": "divider"})

        pages.append({"rows": rows})
        first = False
        if len(pages) > 12:                    # 硬护栏：绝不允许死循环
            break

    if not pages:
        pages = [{"rows": header_rows}]

    # 证件照：只做**几何 + 数据透传**（纯函数不碰 zip/IO）。
    # 显示照片时首页带上这段；不显示/没有照片就完全没有这个键 ——
    # 导出处据此决定"要不要写 media 部件"，避免"看不见却藏在文件里"。
    photo = None
    if has_photo(content, layout):
        raw_photo = (content or {}).get("photo") or {}
        photo = {"x_mm": PHOTO_X_MM, "y_mm": PHOTO_Y_MM,
                 "width_mm": PHOTO_W_MM, "height_mm": PHOTO_H_MM,
                 "mime": raw_photo.get("mime") or "image/jpeg",
                 "data_base64": raw_photo.get("data_base64") or "",
                 "pixel_width": raw_photo.get("width"), "pixel_height": raw_photo.get("height")}

    return {"pages": pages, "theme": theme, "page_w_mm": PAGE_W_MM, "page_h_mm": PAGE_H_MM,
            "band_h_mm": BAND_H_MM, "line_height": effective_line_height(line_height),
            "photo": photo}


def fit_one_page(content, layout, max_pages=1, floor=0.8):
    """
    一页优先（决策 #3）：在 [floor, 用户设定的字号] 之间二分，取「仍能装进 max_pages 页」的**最大**字号。

    为什么要这一步：导出用的高度模型比浏览器的 CSS 保守（每行留一点行后间距、每条留一点条目间距），
    同一份内容在网页预览里刚好一页、在这里可能溢出成两页 —— 表现就是"第 2 页只有几行"，
    用户看起来是排版错乱（2026-10-11 用户报「间距错乱了」）。
    取最大字号 ⇒ 既保证一页，又尽量铺满、不显得空。
    装不下（连 floor 都超）就返回 floor 的方案，允许多页。
    """
    base = dict(layout or {})
    ceiling = base.get("font_scale") or 1.0
    try:
        ceiling = float(ceiling)
    except (TypeError, ValueError):
        ceiling = 1.0
    ceiling = max(floor, min(ceiling, 2.0))

    best = plan(content, dict(base, font_scale=floor))
    best_scale = floor
    low, high = floor, ceiling
    for _ in range(12):
        middle = (low + high) / 2.0
        candidate = plan(content, dict(base, font_scale=round(middle, 3)))
        if len(candidate["pages"]) <= max_pages:
            best, best_scale = candidate, round(middle, 3)
            low = middle
        else:
            high = middle
    best["font_scale_used"] = best_scale
    return best
