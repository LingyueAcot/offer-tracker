# -*- coding: utf-8 -*-
"""
core/excel.py —— 「导入模板.xlsx」的解析规则（纯函数）

【为什么单独一个文件】
Excel 导入是**唯一会读用户文件、又往真库写**的功能，而且是全项目唯一用到可选依赖
openpyxl 的地方。2026-09-22 体检时它的自检数是 **0**：461 行里"哪一列是什么""进展明细怎么拆"
"哪些行要跳过"这些规则，改错了没有任何东西会响。
搬出来的这部分**不碰 openpyxl**：调用方把工作表读成一行行单元格（list of list），
这里只做判断；这样测试喂几个 Python 列表就能跑，不用装库、不用造 xlsx。

【规矩】同 core/talks.py：不许 import server / sqlite3 / urllib / …，不许 open() 文件。
"""

import re

from core.values import _parse_excel_date

SHEET_APPLICATIONS = "投递记录"      # 主表：一行 = 一条投递
SHEET_TALKS = "宣讲会"               # 可选表：一行 = 一场宣讲会
SAMPLE_PREFIX = "示例"               # 模板里的示例行：公司名以它开头就整行跳过


def pick_sheet_name(names, wanted):
    """
    按名字挑工作表（两边都去空格），找不到返回 None —— 调用方自己决定退而求其次用哪张。
    """
    for name in names:
        if str(name).strip() == wanted:
            return name
    return None


def header_of(row):
    """表头行 → 去掉两边空白的字符串列表（单元格可能是数字 / None）。"""
    return [str(cell).strip() if cell is not None else "" for cell in row]


def cell_text(row, index):
    """按列号取单元格文字：列不存在 / 越界 / 空单元格都给空串，不抛异常。"""
    if index is None or index >= len(row):
        return ""
    cell = row[index]
    return "" if cell is None else str(cell).strip()


def is_sample(company):
    """示例行 / 空行：公司名空着、或者以「示例」开头。"""
    return (not company) or company.startswith(SAMPLE_PREFIX)


def _column_of(header, names):
    for name in names:
        if name in header:
            return header.index(name)
    return None


def application_columns(header):
    """
    「投递记录」表的列号。每个字段都给一串别名，因为用户可能自己改过表头。
    只有「公司」是必需的（调用方负责报错）。
    """
    return {
        "company": _column_of(header, ("公司", "企业", "公司名称")),
        "category": _column_of(header, ("类别", "类型")),
        "position": _column_of(header, ("岗位", "职位")),
        "date": _column_of(header, ("投递日期", "投递时间", "时间")),
        "channel": _column_of(header, ("投递渠道", "渠道")),
        "link": _column_of(header, ("投递链接", "链接")),
        "notes": _column_of(header, ("备注",)),
        "stage": _column_of(header, ("当前阶段", "阶段")),
        "result": _column_of(header, ("当前结果", "结果")),
        "detail": _column_of(header, ("进展明细", "进展", "进度")),
    }


def talk_columns(header):
    """「宣讲会」表的列号（这张表是可选的，公司列是必需的）。"""
    return {
        "date": _column_of(header, ("日期", "宣讲会日期")),
        "company": _column_of(header, ("公司", "企业", "公司名称")),
        "time": _column_of(header, ("时间段", "时间")),
        "location": _column_of(header, ("地点",)),
        "on_site": _column_of(header, ("现场环节", "现场")),
        "status": _column_of(header, ("状态",)),
        "rating": _column_of(header, ("口碑评级", "口碑")),
        "summary": _column_of(header, ("口碑/风险摘要", "口碑摘要", "摘要")),
        "questions": _column_of(header, ("现场必问", "必问")),
        "notes": _column_of(header, ("备注",)),
    }


def parse_detail_events(detail, applied):
    """
    「进展明细」列的格式：**分号或换行**分隔每条进展，每条里用 **竖线** 分成
    「阶段 | 结果 | 日期」。结果省略时按「进行中」，日期省略时用这行的投递日期。

    返回 [(阶段, 结果, 日期), ...]；阶段是空的条目直接丢掉。
    """
    events = []
    if not detail:
        return events
    for part in re.split(r"[;；\n\r]+", detail):
        part = part.strip()
        if not part:
            continue
        pieces = [piece.strip() for piece in re.split(r"[|｜]", part)]
        stage = pieces[0] if pieces else ""
        result = pieces[1] if len(pieces) > 1 and pieces[1] else "进行中"
        event_date = _parse_excel_date(pieces[2]) if len(pieces) > 2 else applied
        if stage:
            events.append((stage, result, event_date))
    return events


def row_events(detail, stage_cell, result_cell, applied):
    """
    一行投递要生成哪些进展：
      1. 有「进展明细」→ 按明细拆（可能多条）；
      2. 没有 → 用「当前阶段 / 当前结果」两列，缺阶段默认「投递」，
         缺结果时「投递」算已完成、别的阶段算进行中（和新增投递弹窗一个规则）。
    """
    events = parse_detail_events(detail, applied)
    if events:
        return events
    stage = stage_cell or "投递"
    result = result_cell
    if not result:
        result = "已完成" if stage == "投递" else "进行中"
    return [(stage, result, applied)]
