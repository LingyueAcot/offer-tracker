# -*- coding: utf-8 -*-
"""
core/packaging.py —— 标准化数据包：导入的检查规则、导出的组装、数据模型说明（纯函数）

【为什么单独一个文件】
三件事原来都埋在 server.py 里，体检时一条测试都没有：
  1. `import_package()` —— **默认是「覆盖」模式**：先把 events / applications / talks 全删了再写。
     它读的是用户给的 JSON，形状不对、字段缺失会怎样，以前没有任何东西验过。
  2. `export_package()` —— 导出数据包，字段漏了以后就恢复不回来。
  3. `schema_description()` —— /api/schema 返回的数据模型说明（2026-09-22 之前一直挂在
     「还没搬的纯函数」名单里）。
搬出来之后，这三条规则都能用普通的 Python 字典测，不用起服务、不用碰真库。

【规矩】同 core/talks.py：不许 import server / sqlite3 / urllib / …，不许 open() 文件。
**时间不在这里取**：导出的时间戳由调用方传进来（`exported_at`），这样函数才是纯的。
"""

from core.resume import (CONTENT_VER, MAX_TITLE, default_content, default_layout,
                         normalize_content, normalize_layout)
from core.values import as_int, clean

SCHEMA_NAME = "resume-tracker"


def validate_package(payload):
    """
    导入数据包的形状检查，返回错误信息（None = 通过）。
    只查「能不能往下走」：是不是字典、有没有 applications、它是不是数组。
    单条记录里缺东西不算错，按下面的规则逐条跳过。
    """
    if not isinstance(payload, dict):
        return "数据格式不正确"
    if "applications" not in payload:
        return "缺少 applications 字段"
    if not isinstance(payload["applications"], list):
        return "applications 必须是数组"
    # 简历（2026-10-10 加）：**可选**。老数据包里没有这个键也必须能导（向后兼容），
    # 只是有的话形状要对。
    if "resumes" in payload and not isinstance(payload["resumes"], list):
        return "resumes 必须是数组"
    return None


def package_entries(payload, fallback_stamp):
    """
    把数据包规整成「可以直接写进库」的样子：
      - 没有公司名的记录整条跳过（库里 company 是必填）；
      - 每条进展补默认值：阶段缺了算「投递」、结果缺了算「进行中」；
      - 时间戳缺了用 fallback_stamp（调用方拿 now_str() 传进来，这样本函数才是纯的）。
    返回 [{应用字段..., "events": [{进展字段...}]}]，顺序和输入一致。
    """
    fallback = fallback_stamp or ""
    out = []
    for item in payload.get("applications") or []:
        if not isinstance(item, dict):
            continue
        company = clean(item.get("company"))
        if not company:
            continue
        stamp = clean(item.get("created_at"), 32) or fallback
        events = []
        for event in item.get("events") or []:
            if not isinstance(event, dict):
                continue
            events.append({
                "stage": clean(event.get("stage"), 200) or "投递",
                "result": clean(event.get("result"), 32) or "进行中",
                "note": clean(event.get("note")),
                "event_date": clean(event.get("event_date"), 32),
                "created_at": clean(event.get("created_at"), 32) or stamp,
                "completed_at": clean(event.get("completed_at"), 32),
            })
        out.append({
            "company": company,
            "category": clean(item.get("category")),
            "position": clean(item.get("position")),
            "applied_date": clean(item.get("applied_date"), 32),
            "channel": clean(item.get("channel"), 200),
            "link": clean(item.get("link"), 2000),
            "notes": clean(item.get("notes")),
            "created_at": stamp,
            "updated_at": clean(item.get("updated_at"), 32) or stamp,
            "events": events,
        })
    return out


def package_stages(payload):
    """
    数据包里带的自定义阶段，规整成 {name, sort_order, is_preset}。
    两种写法都认：{"name": "AI测评", "sort_order": 300} 或者直接一个字符串。
    名字空的丢掉；排序缺省 500（和「自定义阶段」的默认一致）。
    """
    out = []
    for item in payload.get("stages") or []:
        if isinstance(item, dict):
            name = clean(item.get("name"), 200)
            order = as_int(item.get("sort_order")) or 500
            preset = 1 if item.get("is_preset") else 0
        elif isinstance(item, str):
            name, order, preset = clean(item, 200), 500, 0
        else:
            continue
        if name:
            out.append({"name": name, "sort_order": order, "is_preset": preset})
    return out


def package_talks(payload, fallback_stamp):
    """数据包里的宣讲会：没有公司名的跳过；状态缺了算「待参加」；时间戳缺了用 fallback。"""
    out = []
    for talk in payload.get("talks") or []:
        if not isinstance(talk, dict):
            continue
        company = clean(talk.get("company"))
        if not company:
            continue
        stamp = clean(talk.get("created_at"), 32) or (fallback_stamp or "")
        out.append({
            "talk_date": clean(talk.get("talk_date"), 32),
            "company": company,
            "time_slot": clean(talk.get("time_slot"), 100),
            "location": clean(talk.get("location"), 300),
            "on_site": clean(talk.get("on_site"), 50),
            "status": clean(talk.get("status"), 32) or "待参加",
            "rating": clean(talk.get("rating"), 50),
            "summary": clean(talk.get("summary")),
            "questions": clean(talk.get("questions")),
            "notes": clean(talk.get("notes")),
            "created_at": stamp,
            "updated_at": clean(talk.get("updated_at"), 32) or stamp,
        })
    return out


def package_resumes(payload, fallback_stamp):
    """
    数据包里的简历（2026-10-10 加）：规整成「可以直接写进库」的样子。

    正文与排版交给 `core/resume.py` 的归一化规则 —— 和接口保存用的是**同一套**，
    所以数据包里塞了畸形 content 也进不了库（类型错退回默认结构，不报错、不中断导入）。

    返回的 content / layout 是**对象**（不是字符串）：JSON 的编解码在 services/server 那侧做，
    core/ 不许 import json。
    """
    out = []
    for item in payload.get("resumes") or []:
        if not isinstance(item, dict):
            continue
        title = clean(item.get("title"), MAX_TITLE) or "未命名简历"
        content, content_error = normalize_content(item.get("content"))
        layout, layout_error = normalize_layout(item.get("layout"))
        if content_error:
            content = default_content()
        if layout_error:
            layout = default_layout()
        stamp = clean(item.get("created_at"), 32) or (fallback_stamp or "")
        out.append({
            "title": title,
            "is_default": 1 if item.get("is_default") else 0,
            "sort_order": as_int(item.get("sort_order")) or 500,
            "content": content,
            "layout": layout,
            "content_ver": CONTENT_VER,
            "rev": as_int(item.get("rev")) or 1,
            "created_at": stamp,
            "updated_at": clean(item.get("updated_at"), 32) or stamp,
        })
    return out


def export_package(state, schema_version, app_version, exported_at, resumes=None):
    """
    标准化数据包：备份、迁移、后续版本升级都用这个格式。

    ⚠️ `resumes` 要单独传进来（2026-10-10 加）：`state` 里那份简历**只有元信息**
    （不带正文，见 services/state.py 的注释），直接塞进去会导出一堆空壳。
    调用方（server.py）用 `services.resumes.export_all()` 取完整的那一份。
    默认 None → 空数组，保证老调用点（只传四个参数）行为不变。
    """
    return {
        "schema": SCHEMA_NAME,
        "schema_version": schema_version,
        "app_version": app_version,
        "exported_at": exported_at,
        "applications": state["applications"],
        "stages": state["stages"],
        "talks": state["talks"],
        "resumes": list(resumes) if resumes is not None else [],
    }


def csv_header():
    """导出的 CSV 表头（列顺序和 csv_rows() 一一对应）。"""
    return [
        "公司", "类别", "岗位", "投递日期", "投递渠道", "当前阶段", "当前结果",
        "状态", "进展条数", "最近登记时间", "备注", "完整进展", "投递链接",
    ]


def csv_rows(state):
    """
    /api/export.csv 的每一行（不含表头）。「完整进展」一列把整条时间线压成一行文字：
    `阶段(结果) @ 登记时间 备注`，多条之间用 ` | ` 隔开。
    """
    rows = []
    for app in state["applications"]:
        timeline = " | ".join(
            "%s%s @ %s%s"
            % (
                event.get("stage") or "",
                ("(" + event.get("result") + ")") if event.get("result") else "",
                event.get("created_at") or "",
                (" " + event.get("note")) if event.get("note") else "",
            )
            for event in app["events"]
        )
        rows.append([
            app.get("company"),
            app.get("category"),
            app.get("position"),
            app.get("applied_date"),
            app.get("channel"),
            app.get("current_stage"),
            app.get("current_result"),
            app.get("derived_status"),
            app.get("event_count"),
            app.get("last_event_at"),
            app.get("notes"),
            timeline,
            app.get("link"),
        ])
    return rows


def schema_description(schema_version, app_version, storage):
    """
    /api/schema 的说明文本。storage 是 {dir, database, change_log} 三个路径，
    由调用方传进来（这里不认得磁盘）。
    """
    return {
        "schema": SCHEMA_NAME,
        "schema_version": schema_version,
        "app_version": app_version,
        "storage": {
            "dir": storage.get("dir", ""),
            "database": storage.get("database", ""),
            "change_log": storage.get("change_log", ""),
        },
        "entities": {
            "application": {
                "id": "整数，自动生成",
                "company": "字符串，必填",
                "category": "字符串，私企 / 央国企 / 外企…",
                "position": "字符串，岗位名称",
                "applied_date": "字符串，YYYY-MM-DD",
                "channel": "字符串，投递渠道",
                "link": "字符串，投递链接",
                "notes": "字符串，备注",
                "created_at": "字符串，YYYY-MM-DD HH:MM:SS",
                "updated_at": "字符串，YYYY-MM-DD HH:MM:SS",
            },
            "event": {
                "id": "整数，自动生成",
                "application_id": "整数，所属投递记录",
                "stage": "字符串，阶段名称，可自定义",
                "result": "进行中 / 已完成 / 通过 / 待定 / 未通过 / 已放弃",
                "note": "字符串，备注",
                "event_date": "字符串，事件日期 YYYY-MM-DD",
                "created_at": "字符串，登记时间，自动生成且不可修改",
                "completed_at": "字符串，本阶段完成时间；点绿勾或选“已完成”时自动记录",
            },
            "stage": {
                "name": "字符串，阶段名称",
                "sort_order": "整数，排序",
                "is_preset": "0 / 1，是否预设阶段",
            },
            "talk": {
                "id": "整数，自动生成",
                "talk_date": "字符串，宣讲会日期 YYYY-MM-DD",
                "company": "字符串，公司名称，必填",
                "time_slot": "字符串，时间段，例如 14:00 或 下午",
                "location": "字符串，地点 / 校区 / 教室",
                "on_site": "字符串，现场是否有笔面试（有 / 无 / 待确认）",
                "status": "字符串，待参加 / 已参加 / 已投递 / 已面试 / 已放弃",
                "app_id": "整数，状态变成「已投递」时自动同步生成的投递记录 id；0 = 还没同步",
                "rating": "字符串，口碑评级，例如 ✅ 相对正面 / ⚠️ 谨慎 / ❌ 较差",
                "summary": "字符串，口碑与风险摘要",
                "questions": "字符串，现场必问的问题",
                "notes": "字符串，备注",
            },
            "resume": {
                "id": "整数，自动生成",
                "title": "字符串，这份简历的名字（多份简历靠它区分）",
                "is_default": "0 / 1，打开简历视图时默认载入的那一份",
                "sort_order": "整数，列表排序",
                "content": "对象，结构化正文：basics / education / work / military / campus / projects / skills / awards",
                "layout": "对象，排版参数：template / font_scale / line_height / page / section_order / show_photo / max_pages",
                "content_ver": "整数，正文格式版本（现在是 1）",
                "rev": "整数，乐观并发版本；保存时必须带上，对不上返回 409 且不写库",
                "created_at": "字符串，YYYY-MM-DD HH:MM:SS",
                "updated_at": "字符串，YYYY-MM-DD HH:MM:SS",
            },
        },
        "endpoints": {
            "GET /api/state": "当前全部数据",
            "GET /api/export.json": "导出标准化数据包",
            "POST /api/import": "导入标准化数据包（默认覆盖）",
            "POST /api/import_excel": "按「导入模板.xlsx」一键导入（默认追加）",
            "GET /api/template.xlsx": "下载 Excel 导入模板",
            "POST /api/talks": "新增宣讲会",
            "PUT /api/talks/<id>": "修改宣讲会",
            "DELETE /api/talks/<id>": "删除宣讲会",
            "GET /api/changelog": "改动日志",
            "GET /api/resumes": "简历列表（只含元信息，不含正文）",
            "GET /api/resumes/<id>": "读一份完整简历（正文 + 排版 + rev）",
            "POST /api/resumes": "新建简历（第一份自动成为默认）",
            "POST /api/resumes/<id>/duplicate": "复制这份简历",
            "POST /api/resumes/<id>/default": "设为默认简历",
            "PUT /api/resumes/<id>": "保存简历（必须带 rev，对不上返回 409）",
            "DELETE /api/resumes/<id>": "删除简历",
            "GET /api/schema": "本说明",
        },
        "compatibility": "导入时按字段名读取，忽略多余字段；schema_version=1 的字段会保持兼容。",
    }
