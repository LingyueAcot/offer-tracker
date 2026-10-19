# -*- coding: utf-8 -*-
"""
core/resume.py —— 简历的纯逻辑：默认值 / 条目标识 / 校验与归一化

【为什么放 core】
这些是**给同样输入就给同样输出**的规则：默认模板、条目 id 生成、长度与结构校验。
不碰数据库、不碰文件、不碰 json。JSON 的编解码在 `services/resumes.py` 做
（`tools/test_architecture.py` 的 R3 明令 core/ 不许 import json / os / io / pathlib）。

【规矩】
- 不许 import server / store / services（R1）；不许 import os/io/json/pathlib（R3）；
  不许 open()（R4）。改完跑 `python tools\\test_core.py`。
- 校验的语义（与 `docs/简历功能-数据契约.md` 对齐）：
    · 类型不符 / 结构不对 → 返回错误（由 services 转成 400，**不改库**）
    · 未知键            → 忽略（向前兼容）
    · 超长文本          → 按 `clean()` 截断（与 notes 的既有写法一致，不报错）
    · 条目数超上限      → 截断到上限（不报错）
- 日期一律当字符串原样保留：用户怎么写就怎么留，不做解析（避免把 "2024.09 – 2027.06" 弄坏）。
"""

import re

from core import resume_photo

from core.values import as_int, clean
# ── 数据格式版本（content 与 layout 各自独立版本化） ──────────────
CONTENT_VER = 3      # 3 = 加证件照 content.photo；2 = 基本信息改成可自定义的 info 行（最多 8 条）
                     # 老数据缺新键一律按默认值处理（不留 info 就迁移、没有 photo 就是没照片）
LAYOUT_VER = 1

# ── 长度与数量上限 ────────────────────────────────────────────
MAX_TITLE = 60
MAX_SHORT = 200        # basics 各字段 / 单位名 / 角色
MAX_TEXT = 500         # 一条 bullet
MAX_LABEL = 20         # 项目小节标题（"材料合成"）
MAX_SECTION_TEXT = 1000  # 项目小节正文
MAX_ITEMS = 30         # 每个数组最多几条
MAX_BULLETS = 12       # 每条经历的 bullet 上限
MAX_SECTIONS = 8       # 每个项目的小节上限

# ── 板块定义（与用户模板的左/右栏一一对应） ─────────────────────
SECTIONS = ("education", "work", "military", "campus", "projects", "skills", "awards")
ITEM_PREFIX = {
    "education": "edu",
    "work": "work",
    "military": "mil",
    "campus": "cam",
    "projects": "proj",
    "skills": "skill",
    "awards": "award",
}
# 简单条目（只有一句话）：skills / awards
SIMPLE_SECTIONS = ("skills", "awards")
# 自定义板块（2026-10-11 加，外部评审方案 MVP）：用户自己"凭空新增"的板块。
# MVP 只做**文本型**（每条一段文本），条目结构与 skills/awards 一致；
# 身份用 id（cs_1 / cs_2 …），**显示名走 layout.section_titles[id]**（与内置板块同一套规则，
# 前端面板不用为自定义板块写特例）；删除用**软删除**（deleted=true 保留数据、可恢复）。
MAX_CUSTOM_SECTIONS = 8       # 活着的自定义板块上限（软删除的不计入）
CUSTOM_ID_RE = re.compile(r"^[a-z0-9_]{1,24}$")
# 排版里认得出来的自定义键形状（前端/归一化都生成 cs_数字）——
# 这样 _read_layout 拿不到 content 也能把自定义键留在 section_order 里
CUSTOM_KEY_RE = re.compile(r"^cs_[0-9]{1,6}$")


def section_key_ok(key, extra_keys=None):
    """这个键能不能进排版（内置板块 / 自定义 id 形状 / 显式给的 extra_keys）。"""
    value = str(key)
    return (value in SECTIONS or value in (extra_keys or ())
            or bool(CUSTOM_KEY_RE.match(value)))
CUSTOM_KINDS = ("text",)
# 带起止时间的条目
TIMED_SECTIONS = ("education", "work", "military", "campus", "projects")

BASICS_FIELDS = (
    "name", "headline", "phone", "email", "gender",
    "location", "political", "ethnicity", "hometown", "education_level",
)

# ── 基本信息里的「标签：内容」行（2026-10-11 用户明确要求） ──────────────
# 用户原话：「我说的自定义是这些（性别/民族/籍贯…），像这样子【性别】等等的应该最多 8 条」。
# 所以这 8 行是**可改标签、可改内容、可删、可加**的，上限 8 条；姓名与一句话定位仍是独立的顶部行。
# 默认这 8 行对应的就是下面这些键（老数据自动迁移进来）。
MAX_INFO_LINES = 8            # 行数上限（用户指定 8）
MAX_INFO_LABEL = 12           # 标签字数上限
MAX_INFO_VALUE = 60           # 内容字数上限
DEFAULT_INFO = (
    ("电话", "phone"), ("邮箱", "email"), ("性别", "gender"), ("现所在地", "location"),
    ("政治面貌", "political"), ("民族", "ethnicity"), ("籍贯", "hometown"),
    ("最高学历", "education_level"),
)

DEFAULT_SECTION_ORDER = {
    "left": ["education", "skills", "awards"],
    "right": ["military", "projects", "work", "campus"],
}

# 配色默认值：**取自己用户那份 PPTX 原件的实测值**（2026-10-10 提取，见 extract_colors.py）。
# 这里是"默认长什么样"，用户可以在界面上改（改完存进 layout.theme）。
DEFAULT_THEME = {
    "header_bg": "#3B5F82",      # 抬头色块
    "header_text": "#FFFFFF",    # 抬头姓名
    "accent": "#2F6595",         # 板块标题 / 分隔线 / 项目符号
}

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def default_basics():
    return dict((field, "") for field in BASICS_FIELDS)


def default_info():
    """空白简历的 8 行基本信息（标签用默认的，内容留空）。"""
    return [{"id": "info_%d" % (index + 1), "label": label, "value": ""}
            for index, (label, _field) in enumerate(DEFAULT_INFO)]


def default_content():
    """一份空白简历的正文骨架。"""
    content = {"basics": default_basics(), "info": default_info(), "photo": None,
               "custom_sections": []}
    for section in SECTIONS:
        content[section] = []
    return content


def default_layout():
    """默认排版参数：对应用户给的「私企两栏版」模板（A4 满版、两栏）。"""
    return {
        "template": "two-column-private",
        "font_scale": 1.0,
        "line_height": 1.55,
        "page": {"margin_top_mm": 0, "margin_left_mm": 0},
        "section_order": {
            "left": list(DEFAULT_SECTION_ORDER["left"]),
            "right": list(DEFAULT_SECTION_ORDER["right"]),
        },
        # 板块标题可以改（2026-10-10 加）：{"education": "教育背景"}；缺的键用内置标题
        "section_titles": {},
        # 不显示的板块：比如没有部队经历就把它藏起来
        "hidden_sections": [],
        "theme": dict(DEFAULT_THEME),
        "show_photo": False,
        "max_pages": 2,
    }


def deep_clone(value):
    """递归复制（纯函数，不 import copy）—— 用于「复制这份简历」。"""
    if isinstance(value, dict):
        return dict((key, deep_clone(item)) for key, item in value.items())
    if isinstance(value, list):
        return [deep_clone(item) for item in value]
    return value


def next_item_id(prefix, existing_ids):
    """按 `前缀_N` 生成一个没被用过的 id（N 取现有最大值 +1）。"""
    biggest = 0
    used = set()
    for item_id in existing_ids or ():
        text = str(item_id or "")
        used.add(text)
        if text.startswith(prefix + "_"):
            number = as_int(text[len(prefix) + 1:])
            if number and number > biggest:
                biggest = number
    candidate = "%s_%d" % (prefix, biggest + 1)
    while candidate in used:                     # 极端情况下（手改过数据）继续往后找
        biggest += 1
        candidate = "%s_%d" % (prefix, biggest + 1)
    return candidate


def _clean_bullets(raw, where, errors):
    """把 bullets 归一化成字符串数组。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append("%s.bullets 必须是数组" % where)
        return []
    out = []
    for index, bullet in enumerate(raw):
        if bullet is None:
            continue
        if isinstance(bullet, (dict, list)):
            errors.append("%s.bullets[%d] 必须是字符串" % (where, index))
            continue
        text = clean(bullet, MAX_TEXT)
        if text:
            out.append(text)
        if len(out) >= MAX_BULLETS:
            break
    return out


def _normalize_timed_item(raw, section, index, errors):
    where = "%s[%d]" % (section, index)
    if not isinstance(raw, dict):
        errors.append("%s 必须是对象" % where)
        return None
    item = {
        "id": clean(raw.get("id"), 40) or "",
        "org": clean(raw.get("org"), MAX_SHORT) if section != "education" else "",
        "school": clean(raw.get("school"), MAX_SHORT) if section == "education" else "",
        "role": clean(raw.get("role"), MAX_SHORT) if section != "education" else "",
        "degree": clean(raw.get("degree"), MAX_SHORT) if section == "education" else "",
        "major": clean(raw.get("major"), MAX_SHORT) if section == "education" else "",
        "start": clean(raw.get("start"), 40),
        "end": clean(raw.get("end"), 40),
        "bullets": _clean_bullets(raw.get("bullets"), where, errors),
    }
    # 去掉与该板块无关的键，保持 JSON 干净
    if section == "education":
        item.pop("org", None)
        item.pop("role", None)
    else:
        item.pop("school", None)
        item.pop("degree", None)
        item.pop("major", None)
    return item


def _normalize_project(raw, index, errors):
    where = "projects[%d]" % index
    if not isinstance(raw, dict):
        errors.append("%s 必须是对象" % where)
        return None
    sections = []
    raw_sections = raw.get("sections")
    if raw_sections is None:
        raw_sections = []
    if not isinstance(raw_sections, list):
        errors.append("%s.sections 必须是数组" % where)
        raw_sections = []
    for sub_index, sub in enumerate(raw_sections):
        if len(sections) >= MAX_SECTIONS:
            break
        if not isinstance(sub, dict):
            errors.append("%s.sections[%d] 必须是对象" % (where, sub_index))
            continue
        label = clean(sub.get("label"), MAX_LABEL)
        text = clean(sub.get("text"), MAX_SECTION_TEXT)
        if label or text:
            sections.append({"label": label, "text": text})
    return {
        "id": clean(raw.get("id"), 40) or "",
        "name": clean(raw.get("name"), MAX_SHORT),
        "role": clean(raw.get("role"), MAX_SHORT),
        "start": clean(raw.get("start"), 40),
        "end": clean(raw.get("end"), 40),
        "topic": clean(raw.get("topic"), MAX_TEXT),
        "sections": sections,
    }


def _normalize_simple_item(raw, index, errors, section):
    where = "%s[%d]" % (section, index)
    if isinstance(raw, dict):
        text = clean(raw.get("text"), MAX_TEXT)
        item_id = clean(raw.get("id"), 40) or ""
    elif raw is None:
        return None
    elif isinstance(raw, (list,)):
        errors.append("%s 必须是对象或字符串" % where)
        return None
    else:
        text = clean(raw, MAX_TEXT)
        item_id = ""
    if not text:
        return None
    return {"id": item_id, "text": text}


def normalize_content(raw):
    """
    把前端传来的正文归一化成规范结构。

    返回 `(content, None)` 或 `(None, "错误信息")`；错误信息里带字段路径。
    """
    if raw is None:
        return default_content(), None
    if not isinstance(raw, dict):
        return None, "content 必须是对象"

    errors = []
    content = {"basics": default_basics()}

    raw_basics = raw.get("basics")
    if raw_basics is not None:
        if not isinstance(raw_basics, dict):
            return None, "basics 必须是对象"
        for field in BASICS_FIELDS:
            if field in raw_basics:
                content["basics"][field] = clean(raw_basics.get(field), MAX_SHORT)

    # 基本信息行（最多 8 条，2026-10-11 加）：优先用 info；老数据没有这个键就按默认标签迁移过来
    raw_info = raw.get("info")
    info = []
    if isinstance(raw_info, list):
        for entry in raw_info:
            if not isinstance(entry, dict):
                continue
            label = clean(entry.get("label"), MAX_INFO_LABEL)
            value = clean(entry.get("value"), MAX_INFO_VALUE)
            if not label and not value:
                continue
            info.append({"label": label, "value": value})
            if len(info) >= MAX_INFO_LINES:
                break
    else:
        for label, field in DEFAULT_INFO:
            info.append({"label": label, "value": clean(content["basics"].get(field), MAX_INFO_VALUE)})
    content["info"] = [{"id": "info_%d" % (index + 1), "label": row["label"], "value": row["value"]}
                       for index, row in enumerate(info)]
    # 兼容：标签还是默认那几个时顺手写回老字段 —— 老读法（含老导出/第三方）照样能取到电话邮箱
    for row in content["info"]:
        for label, field in DEFAULT_INFO:
            if row["label"] == label:
                content["basics"][field] = row["value"]

    # 证件照（2026-10-11 加）：交给 core/resume_photo 做**纯校验**（base64 严格解码 + JPEG 结构/尺寸）
    photo, photo_error = resume_photo.validate(raw.get("photo"))
    if photo_error:
        return None, "photo：" + photo_error
    content["photo"] = photo

    # 自定义板块：id 稳定（不因改名/挪位而重生）、条目只留非空文本、软删除保留
    raw_custom = raw.get("custom_sections")
    custom = []
    used = set()
    if isinstance(raw_custom, list):
        for index, entry in enumerate(raw_custom):
            if not isinstance(entry, dict):
                continue
            sid = str(entry.get("id") or "").strip()
            if not CUSTOM_ID_RE.match(sid) or sid in used:
                sid = "cs_%d" % (index + 1)
            if sid in used:
                # 保持 cs_N 形状：撞了就往后找一个空号（cs_1_ 这种排版那边认不出来）
                number = 1
                while ("cs_%d" % number) in used:
                    number += 1
                sid = "cs_%d" % number
            used.add(sid)
            items = []
            for row in entry.get("items") or []:
                if not isinstance(row, dict):
                    continue
                item_text = clean(row.get("text"), MAX_SECTION_TEXT)
                if item_text:
                    items.append({"text": item_text})
                if len(items) >= MAX_ITEMS:
                    break
            custom.append({"id": sid, "kind": "text",
                           "deleted": bool(entry.get("deleted")), "items": items})
    # 上限：活着的超过 8 个时，多出来的**标记为已删除**（保留数据，绝不静默丢弃）
    active_seen = 0
    for entry in custom:
        if entry["deleted"]:
            continue
        active_seen += 1
        if active_seen > MAX_CUSTOM_SECTIONS:
            entry["deleted"] = True
    content["custom_sections"] = custom

    for section in SECTIONS:
        raw_items = raw.get(section)
        if raw_items is None:
            raw_items = []
        if not isinstance(raw_items, list):
            return None, "%s 必须是数组" % section

        items = []
        for index, raw_item in enumerate(raw_items):
            if len(items) >= MAX_ITEMS:
                break
            if section in SIMPLE_SECTIONS:
                item = _normalize_simple_item(raw_item, index, errors, section)
            elif section == "projects":
                item = _normalize_project(raw_item, index, errors)
            else:
                item = _normalize_timed_item(raw_item, section, index, errors)
            if item is not None:
                items.append(item)

        # 标识稳定：缺失就补，重复就换一个
        seen = set()
        for item in items:
            item_id = item.get("id") or ""
            if not item_id or item_id in seen:
                item_id = next_item_id(ITEM_PREFIX[section], seen)
                item["id"] = item_id
            seen.add(item_id)

        content[section] = items

    if errors:
        return None, "；".join(errors[:3])
    return content, None


def _clamp_number(value, low, high, fallback):
    number = None
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = None
    if number is None:
        return fallback
    if number < low:
        return low
    if number > high:
        return high
    return round(number, 3)


def custom_ids(content):
    """正文里所有自定义板块的 id（含软删除的）—— 排版层用它判断"这个键是不是自定义板块"。"""
    out = []
    for entry in (content or {}).get("custom_sections") or []:
        if isinstance(entry, dict) and entry.get("id"):
            out.append(str(entry["id"]))
    return out


def custom_items(content, key):
    """按 id 取一个自定义板块的条目（软删除的返回 None）—— 规划层用。"""
    for entry in (content or {}).get("custom_sections") or []:
        if isinstance(entry, dict) and str(entry.get("id")) == str(key):
            if entry.get("deleted"):
                return None
            return list(entry.get("items") or [])
    return None


def custom_section_error(raw):
    """写库前的校验：活着的自定义板块超过上限就报错（**不截断、不写库**）。返回 None 或错误文本。"""
    if not isinstance(raw, dict):
        return None
    entries = raw.get("custom_sections")
    if not isinstance(entries, list):
        return None
    active = [e for e in entries if isinstance(e, dict) and not e.get("deleted")]
    if len(active) > MAX_CUSTOM_SECTIONS:
        return "自定义板块最多 %d 个（现在 %d 个）" % (MAX_CUSTOM_SECTIONS, len(active))
    return None


def normalize_layout(raw, extra_keys=None):
    """
    把排版参数归一化。非法值一律回落到默认值（不让排版参数把导出弄崩）。

    返回 `(layout, None)` 或 `(None, "错误信息")`。
    """
    layout = default_layout()
    if raw is None:
        return layout, None
    if not isinstance(raw, dict):
        return None, "layout 必须是对象"

    template = clean(raw.get("template"), 60)
    if template:
        layout["template"] = template
    layout["font_scale"] = _clamp_number(raw.get("font_scale"), 0.8, 1.4, layout["font_scale"])
    layout["line_height"] = _clamp_number(raw.get("line_height"), 1.2, 2.0, layout["line_height"])
    layout["show_photo"] = bool(raw.get("show_photo"))

    max_pages = as_int(raw.get("max_pages"))
    if max_pages and 1 <= max_pages <= 9:
        layout["max_pages"] = max_pages

    page = raw.get("page")
    if isinstance(page, dict):
        top = as_int(page.get("margin_top_mm"))
        left = as_int(page.get("margin_left_mm"))
        if top is not None and 0 <= top <= 40:
            layout["page"]["margin_top_mm"] = top
        if left is not None and 0 <= left <= 40:
            layout["page"]["margin_left_mm"] = left

    order = raw.get("section_order")
    if isinstance(order, dict):
        cleaned = {}
        for side in ("left", "right"):
            values = order.get(side)
            if isinstance(values, list):
                keep = [str(v) for v in values if section_key_ok(v, extra_keys)]
                if keep:
                    cleaned[side] = keep
        if cleaned:
            layout["section_order"] = {
                "left": cleaned.get("left", list(DEFAULT_SECTION_ORDER["left"])),
                "right": cleaned.get("right", list(DEFAULT_SECTION_ORDER["right"])),
            }

    # 板块标题改名（2026-10-10 加）：只认已知板块，空标题当没改
    titles = raw.get("section_titles")
    if isinstance(titles, dict):
        renamed = {}
        for key, value in titles.items():
            if section_key_ok(key, extra_keys):
                text = clean(value, MAX_LABEL)
                if text:
                    renamed[key] = text
        if renamed:
            layout["section_titles"] = renamed

    # 隐藏板块：只认已知板块（防止前端传个乱的把整份简历弄空）
    hidden = raw.get("hidden_sections")
    if isinstance(hidden, list):
        layout["hidden_sections"] = [str(v) for v in hidden if section_key_ok(v, extra_keys)]

    # 配色：必须是 #RRGGBB，非法值一律保留默认（不让配色把导出弄崩）
    theme = raw.get("theme")
    if isinstance(theme, dict):
        for key in ("header_bg", "header_text", "accent"):
            value = clean(theme.get(key), 7)
            if HEX_RE.match(value or ""):
                layout["theme"][key] = value.upper()
    return layout, None
