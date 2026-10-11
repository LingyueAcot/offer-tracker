# -*- coding: utf-8 -*-
"""
core/talks.py —— 宣讲会：详情页/职位列表解析、公司名、对口初判（纯函数）

【这个包是什么】
从 server.py 里搬出来的**纯函数**：给同样的输入就给同样的输出，
不碰数据库、不碰网络、不碰文件、不读全局状态。搬出来是为了能单独写回归测试
（见 tools/test_core.py），也让 server.py 只剩下「接口 + 读写 + 编排」。

【规矩】
- **不许 import server**，不许 import sqlite3 / urllib / subprocess / socket / threading。
  tools/test_core.py 里有一条静态检查盯着这个。
- 这里的东西改了要跑：python tools\test_core.py，以及根 AGENTS.md 的证据表里对应的那几项。
- 代码是从 server.py **逐字搬过来**的（搬的时候断言过 AST 完全一致），不要顺手改行为。
"""

import re
from core.values import clean

# 公司名里常见的“校园招聘噪声词”，用于清洗出标准公司名
CAMPUS_NOISE_PATTERNS = [
    r"\d{4}\s*年?度?", r"\d{2,4}\s*届",
    r"秋季校园招聘", r"春季校园招聘", r"校园招聘", r"专场招聘会", r"专场宣讲会",
    r"秋招", r"春招", r"招聘宣讲会", r"宣讲会", r"专场", r"招聘简章",
    r"招聘", r"宣讲", r"双选会",
]
# 简章里跟「招什么人」有关的句子才值得摘出来
DETAIL_KEY_HINTS = (
    "岗位", "专业", "学历", "硕士", "本科", "薪", "待遇", "福利", "招聘",
    "要求", "职责", "人数", "工作地点", "培养", "晋升", "五险", "公积金", "offer",
)
# 详情页抓下来的正文里，有大量页脚模板（校区地址 / 办公时间 / 联系电话 / 微信公众号……），
# 这些内容对判断对口没有帮助，塞给 AI 只会稀释注意力，所以按关键词过滤掉。
DETAIL_NOISE_HINTS = (
    "校区", "办公时间", "办公地址", "邮箱", "微信公众号", "就业派遣",
    "校招推文", "招聘宣讲服务", "技术咨询", "点击进入", "手机上观看",
    "宣讲日期", "宣讲时间", "版权", "招聘电话", "咨询电话", "联系电话",
    "联系人", "微信同号",
)
# 详情页会把学校页脚和最后一个单元格黏在一起，这里单独列一份「从哪里截断」的关键词。
# 注意不要用「校区」以外的地名，否则会把「某某省某某市」这种正常的工作城市也截掉。
FOOTER_CUT_HINTS = (
    "大学城校区", "东风路校区", "龙洞校区", "龙  洞", "番禺校区",
    "办公时间", "办公地址", "招聘电话", "咨询电话", "联系电话",
    "微信同号", "就业派遣", "微信公众号", "宣讲日期", "手机上观看",
)
# 职位列表是按单元格平铺下来的，这些是表头和按钮文字，不是岗位信息
JOB_LIST_NOISE = {
    "序号", "职位信息", "需求专业", "操作", "投递简历", "职位名称", "岗位名称",
    "招聘人数", "工作地点", "学历要求", "薪资待遇", "岗位职责", "任职要求",
}
RECRUIT_BRIEF_CHARS = 900   # 每家公司的简章摘录上限（字）
RECRUIT_JOBS_LIMIT = 14     # 每家公司最多列几个岗位

def html_to_lines(html):
    text = re.sub(r"(?is)<script.*?</script>", " ", html)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", "\n", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return [line.strip() for line in text.split("\n") if line.strip()]


def parse_talk_time(text):
    """把「2026-09-16 14:30-16:30（周三）」拆成 (日期, 时间段)。"""
    match = re.match(
        r"(\d{4}-\d{2}-\d{2})\s*(\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2})", text or ""
    )
    if match:
        return match.group(1), match.group(2).replace(" ", "")
    match = re.match(r"(\d{4}-\d{2}-\d{2})\s*(\d{1,2}:\d{2})?", text or "")
    if match:
        return match.group(1), (match.group(2) or "")
    return "", ""


def company_key(name):
    """公司名归一键：去掉「股份有限公司 / 有限公司 / 集团」等后缀，方便匹配。"""
    text = normalize_company(name)
    for suffix in ["股份有限公司", "有限责任公司", "有限公司", "集团", "公司"]:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    # 去掉括号和标点，避免「某某（某某）」和「某某某某」匹配不上
    text = re.sub(r"[\s\u3000（）()、,，.。\-—·'\"“”「」\[\]【】]", "", text)
    return text.strip()


def normalize_company(name):
    """把「某集团·某某科技2027届秋招宣讲会」清洗成「某某科技」。

    ⚠️ 示例里的公司名**一律用虚构的**（某某科技）—— 这份文件会被打进正式版发给别人，
    写真实公司名等于把开发者投过哪些公司夹带出去（2026-09-25 的发布包审查会拦这个）。
    """
    text = (name or "").strip()
    if not text:
        return ""
    # 去掉开头的括号备注，例如「（请主动联系对接学院，020-39322210）某某集团」
    # 以及「【央国企】某某电源技术有限公司」这类前缀标签
    text = re.sub(r"^[（(【\[][^）)】\]]{0,40}[）)】\]]\s*", "", text).strip()
    if not text:
        return (name or "").strip()
    if "\u00b7" in text:
        parts = [p.strip() for p in text.split("\u00b7") if p.strip()]
        if parts:
            text = parts[-1]
    text = re.sub(r"(广东工业大学|大学城校区|龙洞校区|东风路校区|番禺校区).*$", "", text)
    cut = None
    for pattern in CAMPUS_NOISE_PATTERNS:
        match = re.search(pattern, text)
        if match and (cut is None or match.start() < cut):
            cut = match.start()
    if cut is not None and cut > 0:
        text = text[:cut]
    text = text.strip(" -\u2014\u00b7\u3001\uff0c,()\uff08\uff09[]\u3010\u3011")
    return text or (name or "").strip()


def parse_talk_list(html):
    """解析宣讲会列表页（必须先用浏览器渲染，见文件上方说明）。"""
    items = []
    for block in re.findall(r'<ul class="infoList teachinList">(.*?)</ul>', html, re.S):
        link = re.search(r'<a href="([^"]+)"\s+title="([^"]*)"', block)
        if not link:
            continue
        url, title = link.group(1), link.group(2).strip()
        id_match = re.search(r"/id/(\d+)", url)
        type_match = re.search(r'<span class="status-text">([^<]*)</span>', block)
        lis = re.findall(r"<li[^>]*>(.*?)</li>", block, re.S)
        texts = []
        for li in lis:
            plain = re.sub(r"(?s)<[^>]+>", " ", li)
            texts.append(re.sub(r"\s+", " ", plain).strip())
        location = texts[1] if len(texts) > 1 else ""
        talk_date, time_slot = parse_talk_time(texts[2] if len(texts) > 2 else "")
        items.append({
            "gdut_id": id_match.group(1) if id_match else "",
            "raw_title": title,
            "company": title,
            "company_std": normalize_company(title),
            "talk_type": (type_match.group(1).strip() if type_match else ""),
            "location": location,
            "talk_date": talk_date,
            "time_slot": time_slot,
            "source_url": url,
        })
    return items


def parse_talk_detail(html):
    """解析宣讲会详情页：单位性质/行业/规模 + 招聘简章全文 + 职位列表。"""
    lines = html_to_lines(html)
    info = {}
    label_map = {
        "单位性质": "unit_nature",
        "单位行业": "unit_industry",
        "单位规模": "unit_scale",
        "举办地点": "location",
        "宣讲类别": "talk_type",
    }
    for index, line in enumerate(lines):
        label = line.rstrip("：:").strip()
        if label in label_map and index + 1 < len(lines) and not info.get(label_map[label]):
            info[label_map[label]] = lines[index + 1].strip()
    detail_text = ""
    if "宣讲会详情" in lines:
        start = lines.index("宣讲会详情") + 1
        end = len(lines)
        for index in range(start, len(lines)):
            if lines[index] == "职位列表":
                end = index
                break
        detail_text = "\n".join(lines[start:end]).strip()
    positions = ""
    if "职位列表" in lines:
        start = lines.index("职位列表") + 1
        positions = "\n".join(lines[start:start + 150]).strip()
    info["detail_text"] = detail_text[:20000]
    info["positions"] = positions[:6000]
    talk_type = info.get("talk_type") or ""
    if "线下" in talk_type:
        info["talk_type"] = "线下"
    elif "线上" in talk_type:
        info["talk_type"] = "线上"
    return info


def format_job_list(positions_text, limit=RECRUIT_JOBS_LIMIT):
    """
    把详情页抓下来的「职位列表」整理成人能读的按行格式，给 AI 判断对口用。

    原文是平铺的单元格文本，一个岗位固定是这几段：
        序号 -> 岗位名称 -> 薪资 -> 工作城市 -> 全职/兼职 -> 学历要求 -> 专业要求 -> 投递简历
    整理后：
        01 ｜ 岗位名称 ｜ 本科及以上 ｜ 8000-10000 ｜ 某省某市 ｜ 专业：某某工程、…、某某材料与工程

    解析失败（页面结构变了）时返回空列表，调用方会退回使用原始文本。
    """
    if not positions_text:
        return []
    lines = []
    for raw in positions_text.split("\n"):
        line = strip_noise_tail(raw.strip())
        if line:
            lines.append(line)
    jobs = []
    current = None
    for line in lines:
        if line in JOB_LIST_NOISE:
            continue
        # 序号是小整数（如 01 / 1 / 001），出现在它后面的就是新岗位
        if re.match(r"^\d{1,3}$", line):
            if current and current["fields"]:
                jobs.append(current)
            current = {"no": line, "fields": []}
            continue
        if current is None:
            continue
        current["fields"].append(line)
    if current and current["fields"]:
        jobs.append(current)

    formatted = []
    for job in jobs:
        fields = job["fields"]
        name = fields[0]
        rest = fields[1:]
        salary = next(
            (f for f in rest if re.search(r"\d{3,6}\s*[-~－—]\s*\d{3,6}", f) or f in ("面议", "详见简章")),
            "",
        )
        # 学历要求不能把「【本科】材料成型…」这类专业行误判进来，所以先排除冒号/括号开头的专业行
        degree = next(
            (f for f in rest
             if not f.startswith("【") and ("、" not in f)
             and any(k in f for k in ("本科", "硕士", "博士", "大专", "不限学历", "学历不限"))),
            "",
        )
        city = next(
            (f for f in rest if ("、" not in f) and (f.endswith("市") or f.endswith("区") or "省" in f)),
            "",
        )
        # 「不限专业」「专业不限」也是关键信息（说明谁都能投），不能丢
        majors = "、".join(
            f for f in rest
            if f.startswith("【") or "、" in f or "不限专业" in f or "专业不限" in f
        )
        if len(majors) > 220:
            majors = majors[:220] + "…"
        parts = [p for p in (name, degree, salary, city) if p]
        row = "%s ｜ %s" % (job["no"], " ｜ ".join(parts))
        if majors:
            row += " ｜ 专业：" + majors
        formatted.append(row)
        if len(formatted) >= limit:
            break
    return formatted


def brief_from_detail(detail_text, limit=RECRUIT_BRIEF_CHARS):
    """从招聘简章正文里挑出跟「招什么岗位、要什么专业」有关的句子（去掉页脚噪音）。"""
    if not detail_text:
        return ""
    picked, fallback = [], []
    for raw in detail_text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if any(hint in line for hint in DETAIL_NOISE_HINTS):
            continue
        if len(line) > 300:
            line = line[:300] + "…"
        fallback.append(line)
        if any(hint in line for hint in DETAIL_KEY_HINTS):
            picked.append(line)
    text = "\n".join(picked or fallback)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def strip_noise_tail(text):
    """
    在第一次出现「学校页脚关键词」的地方截断。

    详情页抓下来的单元格会把学校联系方式黏在岗位的专业要求后面，
    例如：…、高分子材料与工程、大学城校区：020-39325251（用人单位校园宣讲）…
    不处理的话，这段页脚会被 AI 当成岗位信息读，既占地方又误导。
    """
    if not text:
        return ""
    cut = len(text)
    for hint in FOOTER_CUT_HINTS:
        pos = text.find(hint)
        if 0 <= pos < cut:
            cut = pos
    return text[:cut].strip(" 、，,;；/")


def recruit_digest(talk):
    """
    把一家公司的「招聘需求」整理成给 AI 看的几行文字：
    单位性质 / 行业 / 规模 + 职位列表 + 简章关键段落。

    这段原文会直接塞进调研任务包，AI 据此判断对口——所以这里不做任何筛选和打分，
    只做格式整理，判断权完全交给 AI（也方便你复核它的结论对不对）。
    """
    out = []
    nature = (talk.get("unit_nature") or "").strip()
    industry = (talk.get("unit_industry") or "").strip()
    scale = (talk.get("unit_scale") or "").strip()
    if nature or industry or scale:
        bits = []
        if nature:
            bits.append("单位性质：" + nature)
        if industry:
            bits.append("行业：" + industry)
        if scale:
            bits.append("规模：" + scale)
        out.append("  ｜ ".join(bits))

    jobs = format_job_list(talk.get("positions") or "")
    if jobs:
        out.append("职位列表（岗位 ｜ 学历 ｜ 薪资 ｜ 城市 ｜ 专业要求）：")
        out += ["  " + job for job in jobs]
    else:
        raw_positions = (talk.get("positions") or "").strip()
        if raw_positions:
            # 页面结构变了、没能整理成行，就退回原始文本（截断一下，别把提示词撑爆）
            out.append("职位列表（原始文本，未整理）：")
            out.append("  " + raw_positions[:1200].replace("\n", " / "))

    brief = brief_from_detail(talk.get("detail_text") or "")
    if brief:
        out.append("招聘简章关键段落：")
        out += ["  " + line for line in brief.split("\n")]
    return out


def evaluate_fit(talk, profile):
    """用个人画像给宣讲会做「对口」初判，返回 (等级, 理由)。"""
    text = "\n".join([
        talk.get("detail_text") or "",
        talk.get("positions") or "",
        talk.get("unit_industry") or "",
        talk.get("unit_nature") or "",
        talk.get("research_industry") or "",
        talk.get("company") or "",
    ])
    major_hits = [k for k in profile.get("major_keywords", []) if k in text]
    industry_hits = [k for k in profile.get("target_industries", []) if k in text]
    minus_hits = [k for k in profile.get("minus_keywords", []) if k in text]
    wants_master = ("硕士" in text) or ("研究生" in text)
    if major_hits and wants_master:
        level = "strong"
        reason = "命中专业关键词：%s；学历要求含硕士" % "、".join(major_hits[:4])
    elif major_hits:
        level = "mid"
        reason = "命中专业关键词：%s（未明确硕士）" % "、".join(major_hits[:4])
    elif industry_hits:
        level = "mid"
        reason = "行业相关（%s），但未命中材料/化工专业关键词" % "、".join(industry_hits[:3])
    else:
        level = "low"
        reason = "未命中材料/化工类专业关键词"
    if minus_hits:
        reason += "；注意：%s" % "、".join(minus_hits[:3])
    return level, reason


def talk_values(payload):
    """把前端传来的宣讲会字段整理成统一格式。"""
    return (
        clean(payload.get("talk_date"), 32),
        clean(payload.get("company")),
        clean(payload.get("time_slot"), 100),
        clean(payload.get("location"), 300),
        clean(payload.get("on_site"), 50),
        clean(payload.get("status"), 32) or "待参加",
        clean(payload.get("rating"), 50),
        clean(payload.get("summary")),
        clean(payload.get("questions")),
        clean(payload.get("notes")),
    )


# --------------------------------------------------------------------------- #
# 单位性质 -> 投递类别（2026-09-25 加）
#
# 背景：宣讲会状态变成「已投递」时，程序会在投递列表自动建一条记录。
# 原来那条记录的 `category` 被**写死成「宣讲会」**，于是「数据总览 · 投递类别」
# 那张图里混进一个「宣讲会」—— 它跟「私企 / 央国企 / 外企」不是同一类东西，
# 用户看到的就是"投递类别里怎么有个宣讲会"。
#
# 现在按**宣讲会的单位性质**自动归类。值从哪来：学校就业网**详情页**抓的
# `单位性质` 字段（`parse_talk_detail` 写进 talks.unit_nature），不是猜的。
# 实测库里 130 场全都有值，分布是：
#   其他企业（含民营企业等）99 · 国有企业 15 · 外商投资企业 9 · 其他 5 · 未知 2
#
# **抓不到 / 认不出时返回空字符串**（界面显示「未分类」），绝不硬猜 ——
# 归错类会污染统计，比留空更糟。这条是用户 2026-09-25 明确选的。
# --------------------------------------------------------------------------- #

# 顺序有意义：先判"国企/央企"这种更具体的，再判"民营"这种更泛的。
# 例：「国有控股民营企业」这种两头都沾的，按国企算（用户投它多半图个稳）。
UNIT_NATURE_RULES = (
    # (要归成的类别, 命中这些词就算)
    ("央国企", ("国有", "国资", "央企", "中央企业", "国企", "事业单位", "机关", "部队", "军工")),
    ("外企", ("外商", "外资", "三资", "合资", "独资", "港澳台")),
    ("私企", ("民营", "私营", "股份制", "有限责任", "股份有限", "个体", "其他企业")),
)


def category_from_unit_nature(unit_nature):
    """
    把就业网的「单位性质」原文翻成投递列表用的类别。

    返回 `私企` / `央国企` / `外企`，认不出来返回 `""`（调用方显示「未分类」）。

    ⚠️ 纯函数、无副作用：同样的输入永远同样的输出。所以能单独测（`tools/test_core.py`），
    也能在 `services/` 和前端复用同一套规则，不会两边慢慢跑偏。
    """
    text = clean(unit_nature, 200)
    if not text:
        return ""
    for label, keywords in UNIT_NATURE_RULES:
        for word in keywords:
            if word in text:
                return label
    return ""


# 「投递类别」那张图上**不该出现**的类别（2026-09-25 加）。
# 为什么需要：老数据里可能已经有一条 `category = "宣讲会"`（就是上面说的那个历史遗留），
# 而这一版只是不再**新写**它、并订正掉现有那条。留这份名单是**兜底** ——
# 万一以后还有别的地方写进这种"不是企业性质"的值，图表也不会被它污染。
# ⚠️ 只影响**图表**，不影响数据：筛选条里仍然保留这个选项，
#    否则那些记录会变成"看不见但又存在"的幽灵数据。
NOT_A_COMPANY_CATEGORY = ("宣讲会",)
