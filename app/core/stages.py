# -*- coding: utf-8 -*-
"""
core/stages.py —— 阶段与派生状态（纯函数）

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


def derive_status(events):
    """根据最后一条进展推导当前状态。"""
    if not events:
        return "未登记"
    last = events[-1]
    stage = (last.get("stage") or "").strip()
    result = (last.get("result") or "").strip()
    if result == "未通过" or stage == "已拒绝":
        return "已拒绝"
    if result == "已放弃" or stage == "已放弃":
        return "已放弃"
    if stage == "Offer":
        return "Offer"
    return "进行中"


def attach_derived_fields(apps, events):
    """
    给每条投递补上前端要用的派生字段：
      events / event_count / current_stage / current_result / last_event_at / derived_status

    2026-09-24 从 server.py 的 `build_state()` 里搬出来（纯计算，一行没改行为）。
    搬出来之前这段**没有任何离线测试覆盖** —— 它夹在「开连接读四张表」和「拼响应字典」
    中间，只能靠开着程序手工试。现在 `tools/test_core.py` 能直接喂固定数据验它。

    输入约定：
      apps   —— 投递行（dict）的列表，每条要有 `id`
      events —— 进展行（dict）的列表，每条要有 `application_id`、`stage`、`result`、`created_at`
    返回：同一个 `apps` 列表（**就地补字段**），方便 `build_state()` 直接接着用。

    ⚠️ 「就地改」是有意的：`build_state()` 原来就是就地给 app 加键，调用方紧接着
    就把这个列表塞进响应体。保持这个行为，搬迁才不会改变任何输出。
    分组用的是**出现顺序**（不是按时间重排）—— 调用方必须先把 events 按
    `created_at ASC, id ASC` 排好再传进来，和原来 `build_state()` 里的 SQL 顺序一致。
    """
    grouped = {}
    for event in events:
        grouped.setdefault(event["application_id"], []).append(event)

    for app in apps:
        app_events = grouped.get(app["id"], [])
        app["events"] = app_events
        app["event_count"] = len(app_events)
        if app_events:
            last = app_events[-1]
            app["current_stage"] = last.get("stage") or ""
            app["current_result"] = last.get("result") or ""
            app["last_event_at"] = last.get("created_at") or ""
        else:
            app["current_stage"] = ""
            app["current_result"] = ""
            app["last_event_at"] = ""
        app["derived_status"] = derive_status(app_events)

    return apps
