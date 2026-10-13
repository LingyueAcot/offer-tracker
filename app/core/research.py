# -*- coding: utf-8 -*-
"""
core/research.py —— 企业调研：任务包文本解析、结果字段解析（纯函数）

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
from core.talks import normalize_company

# 调研结果里认识的字段名（只有这些词开头才算新字段，其余行算上一字段的续行）
RESEARCH_FIELDS = [
    "口碑评级", "一句话总评", "行业类型",
    # —— 对口判断（调研任务包里会附上该公司的招聘需求原文，让 AI 直接判）
    "对口判断", "对口岗位", "对口说明",
    # —— 第一优先：研发 / 工艺岗的真实待遇与员工体验
    "研发工艺岗薪资", "薪资与结构", "加班与工时", "员工真实体验", "福利与补贴",
    "应届生转正", "应届生相关",
    # —— 第二、第三优先
    "公司基本面", "其他岗位", "负面与媒体", "社交平台",
    "风险建议", "现场必问", "来源",
    # 兼容早期格式
    "校招相关", "待遇与节奏",
]

def parse_research_text(text):
    """
    解析用户粘贴回来的调研结果。

    格式：`===公司：xxx===` 开始、`---结束---` 结束；字段名后用中文冒号。
    字段内容允许换行：只有「认识的字段名」开头的行才算新字段，
    其余行（通常以「- 」开头）都接到上一个字段里，
    这样 DeepSeek 写的多行评价（带来源和原话）也能完整导入。
    """
    entries = []
    known_fields = set(RESEARCH_FIELDS)
    pattern = re.compile(
        r"=+\s*公司\s*[：:]\s*(.+?)\s*=+\s*\n(.*?)(?=\n\s*=+\s*公司\s*[：:]|\Z)", re.S
    )
    for match in pattern.finditer(text or ""):
        entry = {"company": match.group(1).strip()}
        current = None
        for raw_line in match.group(2).split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if re.match(r"^-{2,}\s*结束\s*-{2,}$", line):
                break
            field = re.match(r"^([^：:]{1,12})[：:]\s*(.*)$", line)
            if field and field.group(1).strip() in known_fields:
                current = field.group(1).strip()
                entry[current] = field.group(2).strip()
            elif current:
                entry[current] = (entry[current] + "\n" + line).strip()
        if entry.get("company"):
            entries.append(entry)
    return entries


def _match_talks(entry, talks):
    """
    找出这条调研结果该写到哪些宣讲会记录上。

    注意：**同一家公司可能有多场宣讲会**（例如某某科技同一家公司有两场），
    所以这里要返回「全部」匹配到的记录；只更新第一条的话，
    另一场就看不到评价（这就是「导入后不显示」的原因）。
    匹配优先级：标准公司名精确 → 公司名精确 → 互相包含（取该层级里全部命中）。
    """
    company = (entry.get("company") or "").strip()
    std = normalize_company(company)
    if not std:
        return []
    exact = [t for t in talks if (t.get("company_std") or "").strip() == std]
    if exact:
        return exact
    by_name = [t for t in talks if (t.get("company") or "").strip() == company]
    if by_name:
        return by_name
    loose = []
    for talk in talks:
        talk_std = (talk.get("company_std") or talk.get("company") or "").strip()
        if talk_std and (talk_std in std or std in talk_std):
            loose.append(talk)
    return loose


def _fit_level_from_text(value):
    """
    把 AI 输出的「✅ 对口 / ⚠️ 部分对口 / ⚪ 不对口」翻译成程序内部的等级。

    内部等级沿用关键词初判那一套：strong / mid / low，
    这样看板的配色、排序、筛选都不用改。
    「不对口」要先判断，因为「部分对口」这个词里也含有「对口」两个字。
    """
    text = (value or "").strip()
    if not text:
        return ""
    if "不对口" in text:
        return "low"
    if "部分" in text:
        return "mid"
    if "对口" in text:
        return "strong"
    return ""


def _split_position_note(line):
    """把岗位里的『——注：xxx』拆出来单独放到备注。"""
    if "——注：" in line:
        position, note = line.split("——注：", 1)
        return position.strip(), note.strip()
    return line.strip(), ""


def _merge_notes(*parts):
    return "；".join([p.strip() for p in parts if p and p.strip()])
