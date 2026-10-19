# -*- coding: utf-8 -*-
"""
core/values.py —— 基础值处理（纯函数）

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
from datetime import date, datetime

def clean(value, limit=20000):
    if value is None:
        return ""
    text = str(value).replace("\x00", "").strip()
    if len(text) > limit:
        text = text[:limit]
    return text


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _parse_excel_date(value):
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return ""
    match = re.match(r"^(\d{4})[.\-/年](\d{1,2})[.\-/月](\d{1,2})", text)
    if match:
        return "%s-%02d-%02d" % (match.group(1), int(match.group(2)), int(match.group(3)))
    match = re.match(r"^(\d{1,2})[.\-/月](\d{1,2})", text)
    if match:
        return "%04d-%02d-%02d" % (datetime.now().year, int(match.group(1)), int(match.group(2)))
    return text
