# -*- coding: utf-8 -*-
"""
services/apps.py —— 投递 / 进展 / 阶段：增删改、拆分、撤销恢复

2026-09-24 从 `server.py` 搬过来（S4）。搬的时候**只改了 import 的来源，
业务逻辑一行没动** —— 事务边界、连锁反馈、审计顺序、返回值形状全部照旧。

为什么搬：`api/apps.py` 要用这 10 个函数，原来只能 `from server import ...`，
那就是 `api/ → server` 的反向依赖。搬进 `services/` 之后方向就正过来了。

【这一域的几个要点（改之前先看）】
- **所有写操作都要记审计**（`log_change`），删除也要、且带删除前的完整数据。
- **连锁反馈**（`create_event`）：「新阶段开始后，之前还处于进行中的阶段自动标记为已完成」
  —— 这条规则在这里，登记一次进展可能写两条审计（create + N 条 auto_complete）。
- **字段级 patch**（`update_event`）：「传了才改，没传的保持原值」。
  这不是可选优化 —— 整条覆盖会把没传的阶段名/备注一起毁掉（2026-09-22 自检踩到过）。
- **拆分**（`split_application`）会把一条多岗位记录拆成多条，并复制进展时间线。

【规矩】
- 不 import `server`（靠 `store` / `core` / `services.audit`）。
- 不许自己 `sqlite3.connect`，一律走 `store_module.db().locked_cursor()`。
- 返回值形状**和搬之前逐字相同**（`api/` 与自检都按那个形状用）。
"""

import store as store_module
from core.research import _merge_notes, _split_position_note
from core.values import as_int, clean, now_str

from services.audit import log_change


def create_application(payload):
    company = clean(payload.get("company"))
    if not company:
        return None, "公司名称不能为空"
    stamp = now_str()
    with store_module.db().locked_cursor() as cur:
        cur.execute(
            """
            INSERT INTO applications
                (company, category, position, applied_date, channel, link, notes, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                company,
                clean(payload.get("category")),
                clean(payload.get("position")),
                clean(payload.get("applied_date"), 32),
                clean(payload.get("channel"), 200),
                clean(payload.get("link"), 2000),
                clean(payload.get("notes")),
                stamp,
                stamp,
            ),
        )
        app_id = cur.lastrowid
        stage = clean(payload.get("stage"), 200)
        if stage:
            result = clean(payload.get("result"), 32)
            if not result:
                result = "已完成" if stage == "投递" else "进行中"
            completed_at = stamp if result == "已完成" else ""
            cur.execute(
                """
                INSERT INTO events (application_id, stage, result, note, event_date, created_at, completed_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    app_id,
                    stage,
                    result,
                    clean(payload.get("event_note")),
                    clean(payload.get("applied_date"), 32),
                    stamp,
                    completed_at,
                ),
            )
        # before/after 都在提交前读，事务边界与原来一致（先提交，再写日志）
        after = store_module.db().app_dict(cur, app_id)
    log_change(
        "create",
        "application",
        app_id,
        "新增投递：%s · %s" % (company, clean(payload.get("position")) or "未填写岗位"),
        before=None,
        after=after,
    )
    return app_id, None

def update_application(app_id, payload):
    with store_module.db().locked_cursor() as cur:
        row = cur.execute("SELECT id FROM applications WHERE id=?", (app_id,)).fetchone()
        if not row:
            return "记录不存在"
        before = store_module.db().app_dict(cur, app_id)
        cur.execute(
            """
            UPDATE applications SET
                company=?, category=?, position=?, applied_date=?,
                channel=?, link=?, notes=?, updated_at=?
            WHERE id=?
            """,
            (
                clean(payload.get("company")) or "未命名公司",
                clean(payload.get("category")),
                clean(payload.get("position")),
                clean(payload.get("applied_date"), 32),
                clean(payload.get("channel"), 200),
                clean(payload.get("link"), 2000),
                clean(payload.get("notes")),
                now_str(),
                app_id,
            ),
        )
        after = store_module.db().app_dict(cur, app_id)
    log_change(
        "update",
        "application",
        app_id,
        "修改投递：%s · %s" % (after.get("company") if after else "", after.get("position") if after else ""),
        before=before,
        after=after,
    )
    return None

def delete_application(app_id):
    with store_module.db().locked_cursor() as cur:
        before = store_module.db().app_dict(cur, app_id)
        events = [dict(r) for r in cur.execute("SELECT * FROM events WHERE application_id=?", (app_id,))]
        cur.execute("DELETE FROM applications WHERE id=?", (app_id,))
    if before:
        log_change(
            "delete",
            "application",
            app_id,
            "删除投递：%s · %s（含 %d 条进展）"
            % (before.get("company"), before.get("position"), len(events)),
            before={"application": before, "events": events},
            after=None,
        )
    return None

def split_application(app_id):
    """
    把一条“多岗位”记录拆成多条独立记录。
    每个岗位保留原来的公司、类别、投递日期，并复制一份已有的进展时间线。
    """
    with store_module.db().locked_cursor() as cur:
        row = cur.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if not row:
            return 0, "记录不存在"
        record = dict(row)
        lines = [ln.strip() for ln in (record.get("position") or "").split("\n") if ln.strip()]
        if len(lines) <= 1:
            return 0, "这条记录只有一个岗位，不需要拆分"
        events = [
            dict(r)
            for r in cur.execute(
                "SELECT * FROM events WHERE application_id=? ORDER BY created_at ASC, id ASC",
                (app_id,),
            )
        ]
        base_notes = record.get("notes") or ""
        stamp = now_str()
        created = []

        first_position, first_note = _split_position_note(lines[0])
        cur.execute(
            "UPDATE applications SET position=?, notes=?, updated_at=? WHERE id=?",
            (first_position, _merge_notes(base_notes, first_note), stamp, app_id),
        )
        created.append(app_id)

        for line in lines[1:]:
            position, note = _split_position_note(line)
            cur.execute(
                """
                INSERT INTO applications
                    (company, category, position, applied_date, channel, link, notes, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.get("company") or "",
                    record.get("category") or "",
                    position,
                    record.get("applied_date") or "",
                    record.get("channel") or "",
                    record.get("link") or "",
                    _merge_notes(base_notes, note),
                    record.get("created_at") or stamp,
                    stamp,
                ),
            )
            new_id = cur.lastrowid
            for event in events:
                cur.execute(
                    """
                    INSERT INTO events (application_id, stage, result, note, event_date, created_at, completed_at)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        new_id,
                        event.get("stage") or "投递",
                        event.get("result") or "进行中",
                        event.get("note") or "",
                        event.get("event_date") or "",
                        event.get("created_at") or stamp,
                        event.get("completed_at") or "",
                    ),
                )
            created.append(new_id)
    log_change(
        "split",
        "application",
        app_id,
        "拆分岗位：%s 拆成 %d 条" % (record.get("company"), len(created)),
        before={"application": record, "events": events},
        after={"created_ids": created},
    )
    return len(created), None

def create_event(payload):
    app_id = as_int(payload.get("application_id"))
    stage = clean(payload.get("stage"), 200)
    if not app_id:
        return None, "缺少投递记录"
    if not stage:
        return None, "请填写阶段名称"
    # 正常登记使用当前时间；只有“撤销删除”恢复时才允许带上原始登记时间
    stamp = clean(payload.get("created_at"), 32) or now_str()
    with store_module.db().locked_cursor() as cur:
        row = cur.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if not row:
            return None, "投递记录不存在"
        company = row["company"]
        # 连锁反馈：新阶段开始后，之前还处于“进行中”的阶段自动标记为“已完成”
        auto_done = [
            dict(r)
            for r in cur.execute(
                "SELECT * FROM events WHERE application_id=? AND result='进行中'", (app_id,)
            )
        ]
        if auto_done:
            cur.execute(
                "UPDATE events SET result='已完成', completed_at=? WHERE application_id=? AND result='进行中'",
                (stamp, app_id),
            )
        result = clean(payload.get("result"), 32) or "进行中"
        completed_at = stamp if result == "已完成" else ""
        cur.execute(
            """
            INSERT INTO events (application_id, stage, result, note, event_date, created_at, completed_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                app_id,
                stage,
                result,
                clean(payload.get("note")),
                clean(payload.get("event_date"), 32),
                stamp,
                completed_at,
            ),
        )
        event_id = cur.lastrowid
        cur.execute("UPDATE applications SET updated_at=? WHERE id=?", (stamp, app_id))
        # 自定义阶段自动入库，方便下次直接选
        cur.execute(
            "INSERT OR IGNORE INTO stages(name, sort_order, is_preset) VALUES(?,?,0)",
                (stage, 500),
        )
        created = store_module.db().event_dict(cur, event_id)
    # 事务边界与原来一致：先提交，再写这一批改动日志（1 条 + N 条自动完成）
    log_change(
        "create",
        "event",
        event_id,
        "登记进展：%s · %s（%s）" % (company, stage, clean(payload.get("result"), 32) or "进行中"),
        before=None,
        after=created,
    )
    for item in auto_done:
        after_item = dict(item)
        after_item["result"] = "已完成"
        after_item["completed_at"] = stamp
        log_change(
            "auto_complete",
            "event",
            item.get("id"),
            "自动完成上一阶段：%s · %s" % (company, item.get("stage")),
            before=item,
            after=after_item,
        )
    return event_id, None

def update_event(event_id, payload):
    with store_module.db().locked_cursor() as cur:
        row = cur.execute("SELECT id FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            return "进展记录不存在"
        before = store_module.db().event_dict(cur, event_id)
        # 【为什么是「传了才改」而不是「整条覆盖」】2026-09-22 补自检时踩到的真坑：
        # 这里原来是整条 UPDATE，少传 stage 会被写成默认值「进展」、少传 note / event_date
        # 会被清空 —— 也就是说「我只想把这一步标成完成」这种最自然的调用
        # （PUT 一个 {"result":"已完成"}）会把阶段名和备注一起毁掉。
        # 页面里那两处调用（点绿勾 / 编辑弹窗）恰好每次都把字段传全了，所以真数据没被伤到
        # （实测 0 条 stage='进展'），但坑留着迟早被踩 —— 自检第一次跑就踩了。
        # 现在：**没传的字段保持原值**，传了就按传的来（要清空就显式传空串）。
        stage = clean(payload.get("stage"), 200) if "stage" in payload else (before.get("stage") or "")
        if not stage:
            return "阶段名称不能为空"
        if "result" in payload:
            result = clean(payload.get("result"), 32) or "进行中"
        else:
            result = before.get("result") or "进行中"
        note = clean(payload.get("note")) if "note" in payload else (before.get("note") or "")
        event_date = (clean(payload.get("event_date"), 32) if "event_date" in payload
                      else (before.get("event_date") or ""))
        completed_at = before.get("completed_at") or ""
        if result == "已完成" and (before.get("result") or "") != "已完成":
            completed_at = now_str()
        elif result != "已完成":
            completed_at = ""
        cur.execute(
            """
            UPDATE events SET stage=?, result=?, note=?, event_date=?, completed_at=? WHERE id=?
            """,
            (stage, result, note, event_date, completed_at, event_id),
        )
        after = store_module.db().event_dict(cur, event_id)
        app_row = cur.execute(
            "SELECT company FROM applications WHERE id=?", (before.get("application_id"),)
        ).fetchone()
        company = app_row["company"] if app_row else ""
    log_change(
        "update",
        "event",
        event_id,
        "修改进展：%s · %s -> %s（%s）"
        % (company, before.get("stage"), after.get("stage"), after.get("result")),
        before=before,
        after=after,
    )
    return None

def delete_event(event_id):
    with store_module.db().locked_cursor() as cur:
        before = store_module.db().event_dict(cur, event_id)
        cur.execute("DELETE FROM events WHERE id=?", (event_id,))
        app_row = None
        if before:
            app_row = cur.execute(
                "SELECT company FROM applications WHERE id=?", (before.get("application_id"),)
            ).fetchone()
    if before:
        company = app_row["company"] if app_row else ""
        log_change(
            "delete",
            "event",
            event_id,
            "删除进展：%s · %s（%s）" % (company, before.get("stage"), before.get("result")),
            before=before,
            after=None,
        )
    return None

def add_stage(payload):
    name = clean(payload.get("name"), 200)
    if not name:
        return "阶段名称不能为空"
    # 这个函数原来直接用 conn.execute()（没建 cursor），所以这里用 locked_conn()
    # 交出连接本身，写法与原来一致；_store 负责提交和归还。
    with store_module.db().locked_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO stages(name, sort_order, is_preset) VALUES(?,?,0)",
            (name, 500),
        )
    log_change("create", "stage", None, "新增常用阶段：%s" % name, before=None, after={"name": name})
    return None

def delete_stage(stage_id):
    with store_module.db().locked_cursor() as cur:
        row = cur.execute("SELECT * FROM stages WHERE id=?", (stage_id,)).fetchone()
        if not row:
            return "阶段不存在"
        if row["is_preset"]:
            return "预设阶段不能删除"
        before = dict(row)
        cur.execute("DELETE FROM stages WHERE id=?", (stage_id,))
    # 事务边界与原来一致：先提交删除，再写改动日志
    log_change(
        "delete",
        "stage",
        stage_id,
        "删除常用阶段：%s" % before.get("name"),
        before=before,
        after=None,
    )
    return None

def restore_application(payload):
    """撤销删除：把删除前的投递 + 全部进展原样写回。

    ⚠️ 入口先把两段结构校验一遍（2026-09-27 修）：`payload` 是 HTTP 请求体，
    `application` / `events` 不一定是界面上发来的那种对象 —— 传字符串、数字、列表、
    或者 `events` 里混进 `null`，原来都会在 `app.get(...)` / `event.get(...)` /
    `for event in events` 上抛 `AttributeError`/`TypeError`，一路冒到 `_run_chain`
    变成 500「后台出错」。现在一律回 400 人话错误，形状不对就不动数据库。
    """
    app = payload.get("application")
    if not isinstance(app, dict):
        return None, "撤销数据格式不对（缺少 application 对象）"
    events = payload.get("events")
    if not events:                     # 与搬之前那句 `or []` 同样宽容：空值一律当没有进展
        events = []
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        return None, "撤销数据格式不对（events 必须是对象数组）"
    company = clean(app.get("company")) or "未命名公司"
    stamp = now_str()
    with store_module.db().locked_cursor() as cur:
        cur.execute(
            """
            INSERT INTO applications
                (company, category, position, applied_date, channel, link, notes, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                company,
                clean(app.get("category")),
                clean(app.get("position")),
                clean(app.get("applied_date"), 32),
                clean(app.get("channel"), 200),
                clean(app.get("link"), 2000),
                clean(app.get("notes")),
                clean(app.get("created_at"), 32) or stamp,
                stamp,
            ),
        )
        new_id = cur.lastrowid
        for event in events:
            cur.execute(
                """
                INSERT INTO events (application_id, stage, result, note, event_date, created_at, completed_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    new_id,
                    clean(event.get("stage"), 200) or "进展",
                    clean(event.get("result"), 32) or "进行中",
                    clean(event.get("note")),
                    clean(event.get("event_date"), 32),
                    clean(event.get("created_at"), 32) or stamp,
                    clean(event.get("completed_at"), 32),
                ),
            )
        after = store_module.db().app_dict(cur, new_id)
    log_change(
        "restore",
        "application",
        new_id,
        "恢复删除：%s" % company,
        before=None,
        after=after,
    )
    return new_id, None
