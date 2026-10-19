# -*- coding: utf-8 -*-
"""
数据库结构（建表 + 老库补字段）—— **唯一允许改表结构的地方**。

【为什么单独一个文件】
server.py 的 init_db() 原来把这一百多行建表 / 补字段的代码和「起服务」混在一起。
搬到这儿是为了两件事：
  1. server.py 只管接口和读写，结构变更只有一处；
  2. 这份 schema 能**单独测**——tools/test_schema.py 会造一个老结构的库，
     跑一遍看它能不能正确补齐、**原有数据还在不在**（这是唯一会碰真结构的路径）。

【谁调用它】
server.py 的 init_db()。而 main() 里是「先备份、再 init_db()」，
所以 ALTER 真出问题时，手里还有升级前那一刻的副本（见 backup.py）。

【改这里的规矩】
- **只加不改**：新字段往 talk_columns 这类映射里加一条，老库下次启动就会自动补上；
  已经发布过的声明不要改（老库不会重跑，改了只会让新老库不一致）。
- 新表一律 CREATE TABLE IF NOT EXISTS。
- 改完必须跑：python tools\test_schema.py（不用开程序、不碰真实数据）。
"""
from __future__ import print_function


def ensure_schema(conn, preset_stages, schema_version, app_version):
    """
    建表 + 给老库补字段。

    **不管连接的生命周期**：不 commit、不 close、不管 LOCK——都由调用方决定，
    这样测试里可以拿一个临时库直接调它。
    """
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS applications (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            company      TEXT NOT NULL,
            category     TEXT DEFAULT '',
            position     TEXT DEFAULT '',
            applied_date TEXT DEFAULT '',
            channel      TEXT DEFAULT '',
            link         TEXT DEFAULT '',
            notes        TEXT DEFAULT '',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            stage          TEXT NOT NULL,
            result         TEXT DEFAULT '进行中',
            note           TEXT DEFAULT '',
            event_date     TEXT DEFAULT '',
            created_at     TEXT NOT NULL,
            completed_at   TEXT DEFAULT '',
            FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            sort_order INTEGER DEFAULT 500,
            is_preset  INTEGER DEFAULT 0
        )
        """
    )
    for name, order in preset_stages:
        cur.execute(
            "INSERT OR IGNORE INTO stages(name, sort_order, is_preset) VALUES(?,?,1)",
            (name, order),
        )
    # 宣讲会（校招宣讲会行程）：一行 = 一家公司的一场宣讲会
    # talk_date 用 YYYY-MM-DD；status 见 TALK_STATUS_OPTIONS；rating 是口碑评级
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS talks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            talk_date  TEXT DEFAULT '',
            company    TEXT NOT NULL,
            time_slot  TEXT DEFAULT '',
            location   TEXT DEFAULT '',
            on_site    TEXT DEFAULT '',
            status     TEXT DEFAULT '待参加',
            rating     TEXT DEFAULT '',
            summary    TEXT DEFAULT '',
            questions  TEXT DEFAULT '',
            notes      TEXT DEFAULT '',
            gdut_id    TEXT DEFAULT '',
            source     TEXT DEFAULT 'manual',
            company_std TEXT DEFAULT '',
            talk_type  TEXT DEFAULT '',
            unit_nature TEXT DEFAULT '',
            unit_industry TEXT DEFAULT '',
            unit_scale TEXT DEFAULT '',
            detail_text TEXT DEFAULT '',
            positions  TEXT DEFAULT '',
            fit_level  TEXT DEFAULT '',
            fit_reason TEXT DEFAULT '',
            fit_manual INTEGER DEFAULT 0,
            fit_by     TEXT DEFAULT '',
            app_id     INTEGER DEFAULT 0,
            research_industry TEXT DEFAULT '',
            research_sources TEXT DEFAULT '',
            research_at TEXT DEFAULT '',
            source_url TEXT DEFAULT '',
            last_synced_at TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    # 备忘录：首页「今天该干什么」上面那几条随手记（提醒自己某公司要跟进、
    # 某个岗位要约面试等）。刻意做得很轻：一条就是一句话 + 一个完成勾。
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            text       TEXT NOT NULL,
            done       INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 500,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # 2026-10-11：`INSERT OR IGNORE` → **UPSERT**。
    # 为什么必须改：老库里这条记录早就存在，OR IGNORE 会让它**迁移完还是旧值**，
    # 于是"库里真实记录的结构版本"永远停留在第一次建库时的数字 —— 用它做兼容判据会既拦住正常迁移、
    # 又放过"旧程序去开新库"（外部评审 复审第 1 条）。现在迁移成功后把真实版本写回去。
    cur.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(schema_version),),
    )
    # 简历：一行 = 一份简历（2026-10-10 加，配合「简历编辑 + PDF 导出」）。
    # content / layout 存 JSON 文本，**正文与排版分开**：换模板、调字号不该碰正文。
    # 格式、校验与并发规则见 docs/简历功能-数据契约.md；纯逻辑在 core/resume.py，
    # JSON 编解码在 services/resumes.py（core/ 不许 import json）。
    # 多份简历靠多行 + is_default；暂不关联 applications（决策 #2）。
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS resumes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL DEFAULT '未命名简历',
            is_default  INTEGER DEFAULT 0,
            sort_order  INTEGER DEFAULT 500,
            content     TEXT DEFAULT '',
            layout      TEXT DEFAULT '',
            content_ver INTEGER DEFAULT 1,
            rev         INTEGER DEFAULT 1,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
        """
    )
    # ⚠️ app_version 用 **REPLACE** 不用 IGNORE：它是「这个库是哪个程序版本建的/用过的」
    # 的记录，不带任何迁移语义（真正管结构的是 schema_version）。
    # 原来写 IGNORE，于是老库永远留着第一次启动时那个版本号 ——
    # 换了版本也永远显示旧的，正是这个项目最烦的那种「文档/标签跟不上实际」。
    cur.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('app_version', ?)",
        (app_version,),
    )
    columns = [row["name"] for row in cur.execute("PRAGMA table_info(events)")]
    if "completed_at" not in columns:
        cur.execute("ALTER TABLE events ADD COLUMN completed_at TEXT DEFAULT ''")
    # 宣讲会：老库补齐新字段（同步 / 详情 / 对口判定用）
    talk_columns = {
        "gdut_id": "TEXT DEFAULT ''",
        "source": "TEXT DEFAULT 'manual'",
        "company_std": "TEXT DEFAULT ''",
        "talk_type": "TEXT DEFAULT ''",
        "unit_nature": "TEXT DEFAULT ''",
        "unit_industry": "TEXT DEFAULT ''",
        "unit_scale": "TEXT DEFAULT ''",
        "detail_text": "TEXT DEFAULT ''",
        "positions": "TEXT DEFAULT ''",
        "fit_level": "TEXT DEFAULT ''",
        "fit_reason": "TEXT DEFAULT ''",
        "fit_manual": "INTEGER DEFAULT 0",
        # fit_by 记录对口等级是谁定的：'' = 关键词自动初判，'research' = 调研结果里的
        # 「对口判断」，'manual' = 用户手动切换。用来在界面上区分来源，也用来避免
        # 调研结果覆盖用户手动设置过的等级。
        "fit_by": "TEXT DEFAULT ''",
        # app_id：这场宣讲会「已投递」之后，自动同步到主页面生成的那条投递记录 id。
        # 0 表示还没同步；记住它是为了「一场宣讲会只同步一次」，避免重复建记录。
        "app_id": "INTEGER DEFAULT 0",
        "research_industry": "TEXT DEFAULT ''",
        "research_sources": "TEXT DEFAULT ''",
        "research_at": "TEXT DEFAULT ''",
        "source_url": "TEXT DEFAULT ''",
        "last_synced_at": "TEXT DEFAULT ''",
    }
    existing_talk_columns = [row["name"] for row in cur.execute("PRAGMA table_info(talks)")]
    for column_name, declaration in talk_columns.items():
        if column_name not in existing_talk_columns:
            cur.execute("ALTER TABLE talks ADD COLUMN %s %s" % (column_name, declaration))
