# -*- coding: utf-8 -*-
"""
core/stats.py —— 首页统计口径（纯函数）

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

from datetime import datetime, timedelta

from core.values import today_str

def compute_stats(apps):
    counts = {"进行中": 0, "Offer": 0, "已拒绝": 0, "已放弃": 0, "未登记": 0}
    by_category = {}
    today = today_str()
    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
    today_events = 0
    week_events = 0
    for app in apps:
        status = app.get("derived_status") or "未登记"
        counts[status] = counts.get(status, 0) + 1
        category = app.get("category") or "未分类"
        by_category[category] = by_category.get(category, 0) + 1
        for event in app.get("events", []):
            day = (event.get("created_at") or "")[:10]
            if day == today:
                today_events += 1
            if day and day >= week_start:
                week_events += 1
    return {
        "total": len(apps),
        "active": counts.get("进行中", 0),
        "offer": counts.get("Offer", 0),
        "rejected": counts.get("已拒绝", 0),
        "dropped": counts.get("已放弃", 0),
        "untouched": counts.get("未登记", 0),
        "today_events": today_events,
        "week_events": week_events,
        "by_category": by_category,
    }
