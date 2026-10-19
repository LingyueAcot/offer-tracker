# -*- coding: utf-8 -*-
"""
services/talks.py —— 宣讲会：增 / 改 / 删 / 「已投递」自动同步 / 补同步

2026-09-24 从 `server.py` 搬过来（S4）。搬的时候**只改了 import 的来源**：
- `store_module.db().app_dict(cur, id)` 改成 `store_module.db().app_dict(cur, id)`（同一份实现，
  原来 server 里的 `_app_dict` 也是转发到 store）；
- 其余业务逻辑一行没动（事务边界、自动同步规则、审计顺序全部照旧）。

【这一域的规则（改之前先看）】
- **只有状态是「已投递」才自动同步**成一条投递记录；「待参加 / 已参加」不算投递。
- 一场只同步一次，同步出来的投递记录 id 记在 `talks.app_id`。
- **投递列表里已有同名公司时只建立链接、不重复新建** —— 否则按企业统计的数量会凭空多一家。
- 新建时自动登记一条「投递」进展并标成「已完成」，和手动新增投递的默认状态一致。
- `update_talk` 是**字段级 patch**：先把 payload 合到现有记录上再交给 `talk_values`，
  否则「只想改个状态」会把日期、地点一起抹掉（2026-09-22 自检实测到过）。

【规矩】
- 不 import `server`（靠 `store` / `core` / `services.audit`）。
"""

import store as store_module
from core.talks import category_from_unit_nature, normalize_company, talk_values
from core.values import as_int, clean, now_str

from services.audit import log_change


def sync_talk_to_application(cur, talk):
    """
    宣讲会状态变成「已投递」时，把这场宣讲会**自动同步成主页面的一条投递记录**。

    规则（都很保守，避免把手动记的数据弄乱）：
      - 只有状态是「已投递」才同步；「待参加 / 已参加」只是去过宣讲会，还不算投递；
      - 一场宣讲会只同步一次，同步出来的投递记录 id 记在 `talks.app_id`；
      - 如果投递列表里**已经有同名公司**的记录，就只把链接指过去、**不重复新建**
        （否则你按企业统计的投递数量会凭空多一家）；
      - 新建时自动登记一条「投递」进展并标成「已完成」，
        和你在主页面手动新增投递的默认状态保持一致；
      - **类别按宣讲会的单位性质自动归类**（2026-09-25 起）：`国有企业` -> 央国企，
        `外商投资企业` -> 外企，`其他企业（含民营企业等）` -> 私企；
        认不出来**留空**（界面显示「未分类」），**不硬猜**。

    返回一个 dict 描述这次做了什么，供接口提示用；不需要同步时返回 None。
    """
    if not talk:
        return None
    if (talk.get("status") or "") != "已投递":
        return None
    talk_id = talk.get("id")
    company = (talk.get("company") or "").strip()
    if not company:
        return None

    # 已经同步过、且那条投递记录还在 -> 什么都不用做
    linked_id = as_int(talk.get("app_id")) or 0
    if linked_id:
        if cur.execute("SELECT id FROM applications WHERE id=?", (linked_id,)).fetchone():
            return None

    talk_date = (talk.get("talk_date") or "").strip()
    where_line = " ".join(
        part for part in (talk_date, talk.get("time_slot") or "", talk.get("location") or "")
        if part
    )
    note = "来自 %s 的宣讲会（自动同步，岗位可在投递列表里编辑补充）" % (
        where_line or "宣讲会"
    )

    # 同名公司已经在投递列表里 -> 只建立链接，不重复新建
    existing = cur.execute(
        "SELECT id FROM applications WHERE company=? ORDER BY id LIMIT 1", (company,)
    ).fetchone()
    if existing:
        cur.execute("UPDATE talks SET app_id=? WHERE id=?", (existing["id"], talk_id))
        log_change(
            "link", "talk", talk_id,
            "宣讲会「已投递」：%s（投递列表里已有同名公司，未重复新建）" % company,
            before=None, after={"app_id": existing["id"], "company": company},
        )
        return {"action": "linked", "app_id": existing["id"], "company": company}

    stamp = now_str()
    # 类别按**宣讲会的单位性质**自动归类（2026-09-25 改）。
    # 原来这里是写死的 "宣讲会"，导致「数据总览 · 投递类别」里混进一个"宣讲会" ——
    # 它跟「私企 / 央国企 / 外企」不是同一类东西。现在按 unit_nature（就业网详情页抓的
    # 单位性质）映射；**认不出来就留空**（界面显示「未分类」），不硬猜 ——
    # 归错类会污染统计，比留空更糟。规则在 core/talks.py，那边有测试。
    category = category_from_unit_nature(talk.get("unit_nature"))
    cur.execute(
        """
        INSERT INTO applications
            (company, category, position, applied_date, channel, link, notes, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            company,
            category,
            "",
            talk_date[:10] or stamp[:10],
            "宣讲会",
            talk.get("source_url") or "",
            note,
            stamp,
            stamp,
        ),
    )
    new_id = cur.lastrowid
    # 「投递」这一步默认就是已完成，和主页面手动新增投递保持一致
    cur.execute(
        """
        INSERT INTO events (application_id, stage, result, note, event_date, created_at, completed_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (new_id, "投递", "已完成", "", talk_date[:10], stamp, stamp),
    )
    cur.execute("UPDATE talks SET app_id=? WHERE id=?", (new_id, talk_id))
    log_change(
        "create", "application", new_id,
        "宣讲会「已投递」自动同步：%s（类别按单位性质归为%s）"
        % (company, category or "未分类"),
        before=None, after=store_module.db().app_dict(cur, new_id),
    )
    return {"action": "created", "app_id": new_id, "company": company,
            "category": category}

def backfill_talk_applications():
    """
    开机补齐：把「状态已经是已投递、但还没同步到投递列表」的宣讲会补同步一次。

    为什么需要它：自动同步是 2026-09-20 才加上的，在那之前手动标成「已投递」的场次
    不会自己补上。这里只处理 `app_id = 0`（从没同步过）的记录——
    你要是把自动生成的那条投递记录删掉了，`app_id` 仍有值，不会被重新建出来。
    """
    created = 0
    with store_module.db().locked_cursor() as cur:
        rows = [
            dict(r)
            for r in cur.execute(
                "SELECT * FROM talks WHERE status='已投递' AND (app_id IS NULL OR app_id=0)"
            )
        ]
        for row in rows:
            result = sync_talk_to_application(cur, row)
            if result and result.get("action") == "created":
                created += 1
    if created:
        print("已把 %d 场「已投递」的宣讲会补同步到投递列表。" % created)
    return created

def create_talk(payload):
    company = clean(payload.get("company"))
    if not company:
        return None, "公司名称不能为空"
    stamp = now_str()
    values = talk_values(payload)
    with store_module.db().locked_cursor() as cur:
        cur.execute(
            """
            INSERT INTO talks
                (talk_date, company, time_slot, location, on_site, status,
                 rating, summary, questions, notes, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            values + (stamp, stamp),
        )
        talk_id = cur.lastrowid
        after = store_module.db().talk_dict(cur, talk_id)
    log_change(
        "create",
        "talk",
        talk_id,
        "新增宣讲会：%s · %s" % (values[0] or "未填日期", company),
        before=None,
        after=after,
    )
    return talk_id, None

def update_talk(talk_id, payload):
    with store_module.db().locked_cursor() as cur:
        before = store_module.db().talk_dict(cur, talk_id)
        if not before:
            return "宣讲会记录不存在"
        # 【和 update_event 同一个坑】talk_values() 会把没传的字段填成默认值（空串），
        # 直接拿它整条 UPDATE，就会把 payload 里没出现的字段清空 ——
        # 「只想改个状态」的一键推进会把日期、地点一起抹掉（2026-09-22 补自检时实测到）。
        # 所以先把 payload 合到现有记录上，再交给 talk_values（它仍负责截断和默认值）。
        merged = dict(before)
        for key, value in (payload or {}).items():
            merged[key] = value
        values = talk_values(merged)
        cur.execute(
            """
            UPDATE talks SET
                talk_date=?, company=?, time_slot=?, location=?, on_site=?,
                status=?, rating=?, summary=?, questions=?, notes=?, updated_at=?
            WHERE id=?
            """,
            values + (now_str(), talk_id),
        )
        # 改了公司名 -> 重新算标准名；手动改了对口等级 -> 记为人工覆盖，之后自动同步不再覆盖它
        standard = normalize_company(payload.get("company") or "")
        if standard:
            cur.execute("UPDATE talks SET company_std=? WHERE id=?", (standard, talk_id))
        fit_override = clean(payload.get("fit_level"), 16)
        if fit_override:
            cur.execute(
                "UPDATE talks SET fit_level=?, fit_manual=1, fit_by='manual' WHERE id=?",
                (fit_override, talk_id),
            )
        # 状态刚变成「已投递」-> 自动同步一条投递记录到主页面
        # （同一场只同步一次；投递列表里已有同名公司时只建立链接，不重复新建）
        sync_talk_to_application(cur, store_module.db().talk_dict(cur, talk_id))
        after = store_module.db().talk_dict(cur, talk_id)
    log_change(
        "update",
        "talk",
        talk_id,
        "修改宣讲会：%s · %s" % (after.get("talk_date") or "未填日期", after.get("company")),
        before=before,
        after=after,
    )
    return None

def delete_talk(talk_id):
    with store_module.db().locked_cursor() as cur:
        before = store_module.db().talk_dict(cur, talk_id)
        cur.execute("DELETE FROM talks WHERE id=?", (talk_id,))
    if before:
        log_change(
            "delete",
            "talk",
            talk_id,
            "删除宣讲会：%s · %s" % (before.get("talk_date") or "未填日期", before.get("company")),
            before=before,
            after=None,
        )
    return None
