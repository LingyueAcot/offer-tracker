# -*- coding: utf-8 -*-
"""
schema.ensure_schema() 的回归测试 —— 不用开程序，也不碰真实数据。

用法：
    python tools\test_schema.py              # 在系统临时目录里造试验场
    python tools\test_schema.py <空目录>      # 指定试验场（受限环境里 mkdtemp 建不出子目录时用）

【为什么有它】
建表 / 给老库补字段（ALTER TABLE）是整个程序里**唯一会改数据库结构**的地方，
也是唯一可能把用户数据弄坏的地方。所以这里造几种库来跑：

  1. 全新空库        —— 表要齐、预设阶段要落库、meta 里要写上版本号
  2. 老结构的库      —— 缺 completed_at、缺 talks 的一堆新字段、没有 stages/meta/notes 表；
                        补完之后**原有数据一行都不能少**
  3. 再跑一遍        —— 幂等：不报错、不重复插、数据不变
  4. 半新半旧的库    —— 只补真正缺的那些字段

（真实数据在 data 目录里，这个脚本一个字节都不会碰。）
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.dont_write_bytecode = True
sys.stdout.reconfigure(encoding="utf-8")

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)
import schema  # noqa: E402
import server  # noqa: E402

ok = True


def check(label, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("[PASS] " if cond else "[FAIL] ") + label + ("  " + extra if extra else ""))


def columns_of(conn, table):
    return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]


def tables_of(conn):
    return [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]


OLD_SCHEMA = """
CREATE TABLE applications (
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
);
CREATE TABLE events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL,
    stage          TEXT NOT NULL,
    result         TEXT DEFAULT '进行中',
    note           TEXT DEFAULT '',
    event_date     TEXT DEFAULT '',
    created_at     TEXT NOT NULL
);
CREATE TABLE talks (
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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def make_old_db(path):
    """造一个老结构的库，塞进三条真实感的数据。"""
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO applications(company, category, position, notes, created_at, updated_at)"
                 " VALUES('某某科技','私企','工艺工程师','老数据不能丢','2026-09-01 10:00:00','2026-09-01 10:00:00')")
    conn.execute("INSERT INTO events(application_id, stage, result, note, created_at)"
                 " VALUES(1,'投递','已完成','','2026-09-01 10:00:00')")
    conn.execute("INSERT INTO talks(talk_date, company, status, created_at, updated_at)"
                 " VALUES('2026-09-16','某某科技','待参加','2026-09-10 10:00:00','2026-09-10 10:00:00')")
    conn.commit()
    conn.close()


scratch = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="schema_test_")
if not os.path.isdir(scratch):
    os.makedirs(scratch)
print("试验场：%s\n" % scratch)

# ---------- 1) 全新空库 ----------
fresh = os.path.join(scratch, "fresh", "applications.db")
os.makedirs(os.path.dirname(fresh))
conn = sqlite3.connect(fresh)
conn.row_factory = sqlite3.Row
schema.ensure_schema(conn, server.PRESET_STAGES, server.SCHEMA_VERSION, server.APP_VERSION)
conn.commit()
names = tables_of(conn)
check("空库：八张表都建出来了",
      all(t in names for t in ("applications", "events", "stages", "talks", "meta", "notes", "resumes")),
      "实际：" + ",".join(sorted(names)))
count = conn.execute("SELECT COUNT(*) FROM stages").fetchone()[0]
check("空库：预设阶段落库", count >= len(server.PRESET_STAGES), "%d 条" % count)
meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
check("空库：meta 里写了 schema_version / app_version",
      meta.get("schema_version") == str(server.SCHEMA_VERSION) and meta.get("app_version") == server.APP_VERSION,
      str(meta))
conn.close()

# ---------- 2) 老结构的库 ----------
old = os.path.join(scratch, "old", "applications.db")
os.makedirs(os.path.dirname(old))
make_old_db(old)
conn = sqlite3.connect(old)
conn.row_factory = sqlite3.Row
before_tables = set(tables_of(conn))
before_events = columns_of(conn, "events")
before_talks = columns_of(conn, "talks")
schema.ensure_schema(conn, server.PRESET_STAGES, server.SCHEMA_VERSION, server.APP_VERSION)
conn.commit()
check("老库：events 原来确实缺 completed_at", "completed_at" not in before_events)
check("老库：补上了 completed_at", "completed_at" in columns_of(conn, "events"))
missing = [c for c in ("gdut_id", "source", "company_std", "fit_by", "app_id", "last_synced_at")
           if c not in columns_of(conn, "talks")]
check("老库：talks 的新字段都补上了", not missing, "仍缺：" + ",".join(missing) if missing else "")
check("老库：新建了缺失的表（stages / meta / notes / resumes）",
      all(t in tables_of(conn) for t in ("stages", "meta", "notes", "resumes")),
      "原来有：" + ",".join(sorted(before_tables)))

app = conn.execute("SELECT * FROM applications").fetchone()
check("老库：投递数据一条没丢、内容没变",
      app is not None and app["company"] == "某某科技" and app["notes"] == "老数据不能丢",
      str(dict(app) if app else None)[:90])
event = conn.execute("SELECT * FROM events").fetchone()
check("老库：进展数据还在，补的字段是空默认值",
      event is not None and event["stage"] == "投递" and event["completed_at"] == "")
talk = conn.execute("SELECT * FROM talks").fetchone()
check("老库：宣讲会数据还在，补的字段是默认值",
      talk is not None and talk["company"] == "某某科技" and talk["gdut_id"] == ""
      and talk["source"] == "manual")

# ---------- 3) 幂等：再跑一遍 ----------
snapshot = (conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM stages").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM talks").fetchone()[0],
            len(columns_of(conn, "talks")))
try:
    schema.ensure_schema(conn, server.PRESET_STAGES, server.SCHEMA_VERSION, server.APP_VERSION)
    conn.commit()
    again = (conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM stages").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM talks").fetchone()[0],
             len(columns_of(conn, "talks")))
    check("再跑一遍：不报错、数据与字段数都不变", snapshot == again, "%s vs %s" % (snapshot, again))
except Exception as exc:
    check("再跑一遍：不报错、数据与字段数都不变", False, repr(exc))
conn.close()

# ---------- 4) 半新半旧 ----------
half = os.path.join(scratch, "half", "applications.db")
os.makedirs(os.path.dirname(half))
make_old_db(half)
conn = sqlite3.connect(half)
conn.row_factory = sqlite3.Row
conn.execute("ALTER TABLE events ADD COLUMN completed_at TEXT DEFAULT ''")
conn.execute("ALTER TABLE talks ADD COLUMN gdut_id TEXT DEFAULT ''")
conn.execute("ALTER TABLE talks ADD COLUMN source TEXT DEFAULT 'manual'")
conn.commit()
had = len(columns_of(conn, "talks"))
schema.ensure_schema(conn, server.PRESET_STAGES, server.SCHEMA_VERSION, server.APP_VERSION)
conn.commit()
now = columns_of(conn, "talks")
# talks 一共要补 19 个字段，其中 gdut_id / source 上面已经手工加过 -> 这次只该补 17 个
check("半新半旧：只补缺的字段，已有的不动",
      "completed_at" in columns_of(conn, "events") and len(now) == had + 17,
      "talks 从 %d 个字段变成 %d 个（期望 %d）" % (had, len(now), had + 17))
check("半新半旧：数据还在", conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1)
conn.close()

shutil.rmtree(scratch, ignore_errors=True)
print("\n结果：" + ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
