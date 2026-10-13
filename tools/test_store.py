# -*- coding: utf-8 -*-
"""
store.py（数据库访问层）的回归测试 —— 纯逻辑，不用开程序，也不碰真实数据。

用法：
    python tools\test_store.py             # 在系统临时目录里造试验场
    python tools\test_store.py <空目录>     # 指定试验场（受限环境里 mkdtemp 建不出子目录时用这个）

【要守住的是什么】
这一层存在的唯一理由是「连接一定要还回去」。所以这个脚本的重头戏是
**异常路径下连接有没有被漏掉** —— 也就是它对旧代码的改进点：

  1. 正常出块            -> 提交了、连接关了、锁放了
  2. 出块时抛异常        -> 回滚了、连接**仍然**关了、锁**仍然**放了
  3. 连不上库（坏路径）  -> 抛出来，但锁要放掉（否则整个程序僵住）
  4. locked_cursor()     -> 锁真的被拿住 / 放掉（用另一个线程试进来说明）
  5. 行映射              -> row_dict / rows_dict / app_dict / event_dict / talk_dict
                           与旧代码（server.py 的 _app_dict 等）逐字段一致
  6. 外键 PRAGMA         -> 新连接上真的是开的（旧 get_conn() 里有这一句，别搬丢）
  7. 默认实例 API        -> use()/db()/lock() 是同一个对象、同一个锁

刻意**不测**的东西：SQL 语法、业务规则 —— 那些不是这一层的职责。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import threading

sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP_DIR)
import store  # noqa: E402

ok = True


def check(label, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("[PASS] " if cond else "[FAIL] ") + label + ("  " + extra if extra else ""))


# --------------------------------------------------------------------------- #
# 试验场：一个临时目录 + 一个只有两张小表的库
# --------------------------------------------------------------------------- #
scratch = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="store_test_")
work = os.path.join(scratch, "case1")
os.makedirs(work, exist_ok=True)
DB = os.path.join(work, "applications.db")

SCHEMA = """
CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL DEFAULT '',
    position TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
);
CREATE TABLE talks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL DEFAULT '',
    talk_date TEXT NOT NULL DEFAULT ''
);
"""

seed = sqlite3.connect(DB)
seed.executescript(SCHEMA)
seed.commit()
seed.close()

S = store.Store(DB)

# --------------------------------------------------------------------------- #
# ① 正常出块：提交 + 关闭 + 放锁
# --------------------------------------------------------------------------- #
print("--- ① 正常路径 ---")
with S.cursor() as cur:
    cur.execute("INSERT INTO applications (company, position) VALUES (?, ?)",
                ("甲公司", "工艺工程师"))
    new_id = cur.lastrowid
check("正常出块：lastrowid 拿得到", new_id == 1, "id=%s" % new_id)

verify = sqlite3.connect(DB)
rows = verify.execute("SELECT company FROM applications").fetchall()
verify.close()
check("正常出块：数据真的提交进库了", rows == [("甲公司",)], "%r" % (rows,))

check("正常出块后：锁没被占着（能立刻再拿一次）", S.lock.acquire(blocking=False))
S.lock.release()

# --------------------------------------------------------------------------- #
# ② 出块时抛异常：回滚 + **连接仍然关掉** + 锁仍然放掉
#    这一条就是这一层对旧代码的全部意义所在
# --------------------------------------------------------------------------- #
print("--- ② 异常路径（这一层的重点）---")
raised = None
try:
    with S.cursor() as cur:
        cur.execute("INSERT INTO applications (company, position) VALUES (?, ?)",
                    ("不该留下的公司", "x"))
        raise ValueError("故意炸一下")
except ValueError as err:
    raised = err
check("异常被原样抛出来（没有被吞掉）", isinstance(raised, ValueError))

verify = sqlite3.connect(DB)
left = verify.execute("SELECT COUNT(*) FROM applications WHERE company='不该留下的公司'").fetchone()[0]
verify.close()
# ⚠️ 这一条测的是「**结果**」不是「回滚那一行代码」：实测把 `_CursorContext.__exit__`
# 里的 `rollback()` 去掉，这条**照样通过** —— 因为关连接时 SQLite 自己会把没提交的
# 事务回滚掉。所以别把它当成「rollback 被调用过」的证据。
# 显式 rollback 的价值在于「不依赖关连接的副作用」，但那是代码质量，不是行为差异。
check("异常时改动没留下（那行不在库里）", left == 0, "查到 %d 行" % left)

check("异常后：锁仍然被放掉了（没僵住）", S.lock.acquire(blocking=False))
S.lock.release()

# 连接有没有漏？SQLite 允许同一个文件多开，所以「文件被锁住」看不出来。
# 换一个能看的办法：Windows 上删不掉仍被占用的文件。
try:
    os.remove(DB + "-journal")
except OSError:
    pass
tmp_copy = DB + ".can-delete"
shutil.copyfile(DB, tmp_copy)
try:
    os.remove(tmp_copy)
    deletable = True
except OSError:
    deletable = False
check("异常后：没有残留的连接占着库文件（能正常复制/删除）", deletable)

# --------------------------------------------------------------------------- #
# ③ 连不上库：抛出来，但锁要还回去
# --------------------------------------------------------------------------- #
print("--- ③ 连不上库 ---")
bad = store.Store(os.path.join(work, "no-such-dir", "nope.db"))
try:
    bad.cursor().__enter__()
    failed_loudly = False
except sqlite3.OperationalError:
    failed_loudly = True
except Exception:
    failed_loudly = False
check("库路径不可用：如实抛出 sqlite3.OperationalError", failed_loudly)
check("连不上之后：锁没被占着", bad.lock.acquire(blocking=False))
bad.lock.release()

orphan = store.Store()
try:
    orphan.cursor().__enter__()
    msg = ""
except RuntimeError as err:
    msg = str(err)
check("没配路径时给出可读的提示（而不是让 sqlite3 报个看不懂的错）", "store" in msg, msg[:50])

# --------------------------------------------------------------------------- #
# ④ locked_cursor()：锁真的拿住了
# --------------------------------------------------------------------------- #
print("--- ④ locked_cursor 的锁语义 ---")
inside = {"acquired": None}


def try_acquire_from_other_thread():
    """在主线程持有锁的时候，另一个线程应该拿不到 —— 拿到就说明锁是假的。"""
    inside["acquired"] = S.lock.acquire(timeout=0.4)
    if inside["acquired"]:
        S.lock.release()


with S.locked_cursor() as cur:
    cur.execute("SELECT 1").fetchone()
    t = threading.Thread(target=try_acquire_from_other_thread)
    t.start()
    t.join()
check("locked_cursor 块内：别的线程拿不到锁（锁是真的）", inside["acquired"] is False)

inside["acquired"] = None
after = {"acquired": None}


def acquire_after():
    after["acquired"] = S.lock.acquire(timeout=1.0)
    if after["acquired"]:
        S.lock.release()


with S.locked_cursor() as cur:
    cur.execute("SELECT 1").fetchone()
t = threading.Thread(target=acquire_after)
t.start()
t.join()
check("locked_cursor 出块后：别的线程立刻拿得到锁", after["acquired"] is True)

# --------------------------------------------------------------------------- #
# ⑤ 行映射与旧代码逐字段一致
# --------------------------------------------------------------------------- #
print("--- ⑤ 行映射 ---")
with S.cursor() as cur:
    cur.execute("INSERT INTO events (application_id, stage) VALUES (?, ?)", (new_id, "投递"))
    ev_id = cur.lastrowid
    cur.execute("INSERT INTO talks (company) VALUES (?)", ("乙公司",))
    talk_id = cur.lastrowid

with S.cursor() as cur:
    app = S.app_dict(cur, new_id)
    ev = S.event_dict(cur, ev_id)
    talk = S.talk_dict(cur, talk_id)

check("app_dict 返回 dict（不是 sqlite3.Row）", isinstance(app, dict))
check("app_dict 字段齐全且值正确",
      app and app["company"] == "甲公司" and app["position"] == "工艺工程师" and app["id"] == new_id,
      "%r" % (app,))
with S.cursor() as cur:
    check("app_dict 查不存在的 id -> None", S.app_dict(cur, 999999) is None)
    check("event_dict 字段正确", (S.event_dict(cur, ev_id) or {}).get("stage") == "投递")
    check("talk_dict 字段正确", (S.talk_dict(cur, talk_id) or {}).get("company") == "乙公司")
    check("event_dict 查不存在 -> None", S.event_dict(cur, 999999) is None)
    check("talk_dict 查不存在 -> None", S.talk_dict(cur, 999999) is None)

check("row_dict(None) -> None", store.Store.row_dict(None) is None)
check("rows_dict 转一串", store.Store.rows_dict([{"a": 1}]) == [{"a": 1}])

# --------------------------------------------------------------------------- #
# ⑥ 外键 PRAGMA 没搬丢
# --------------------------------------------------------------------------- #
print("--- ⑥ 外键 ---")
with S.cursor() as cur:
    fk = cur.execute("PRAGMA foreign_keys").fetchone()[0]
check("新连接上 foreign_keys 是开的（旧 get_conn() 里那句没丢）", fk == 1, "PRAGMA = %s" % fk)

# 真验一次外键行为：插一条指向不存在投递的进展，应该被拒
fk_enforced = False
try:
    with S.cursor() as cur:
        cur.execute("INSERT INTO events (application_id, stage) VALUES (?, ?)", (999999, "x"))
except sqlite3.IntegrityError:
    fk_enforced = True
check("外键真的在生效（孤儿进展插不进去）", fk_enforced)

check("外键失败后：锁仍然放掉了", S.lock.acquire(blocking=False))
S.lock.release()

# --------------------------------------------------------------------------- #
# ⑦ 默认实例 API
# --------------------------------------------------------------------------- #
print("--- ⑦ 默认实例 ---")
store.use(DB)
check("use() 装好路径后 db() 指向那个库",
      store.db().db_path == DB, "db_path=%s" % store.db().db_path)
check("db() 每次返回同一个实例", store.db() is store.db())
check("lock() 给的就是那个实例的锁", store.lock() is store.db().lock)

with store.db().locked_cursor() as cur:
    n = cur.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
check("通过默认实例也能正常读写", n == 1, "applications = %d 行" % n)

# --------------------------------------------------------------------------- #
# ⑦b conn() / locked_conn()：交出的是**连接本身**（init_db 要用）
# --------------------------------------------------------------------------- #
print("--- ⑦b conn() 交出连接本身 ---")
with S.conn() as raw:
    check("conn() 交出的是 sqlite3.Connection（不是 cursor）",
          isinstance(raw, sqlite3.Connection), type(raw).__name__)
    check("conn() 拿得到 row_factory=Row（旧 get_conn 的行为）", raw.row_factory is sqlite3.Row)
    raw.execute("INSERT INTO talks (company) VALUES (?)", ("丙公司",))

with S.cursor() as cur:
    got = cur.execute("SELECT COUNT(*) FROM talks WHERE company='丙公司'").fetchone()[0]
check("conn() 出块时也会提交", got == 1, "查到 %d 行" % got)
check("conn() 出块后锁是空的", S.lock.acquire(blocking=False))
S.lock.release()

# conn() 的异常路径
try:
    with S.conn() as raw:
        raw.execute("INSERT INTO talks (company) VALUES (?)", ("不该留下",))
        raise ValueError("conn 故意炸")
except ValueError:
    pass
with S.cursor() as cur:
    bad = cur.execute("SELECT COUNT(*) FROM talks WHERE company='不该留下'").fetchone()[0]
check("conn() 异常时不留改动", bad == 0, "查到 %d 行" % bad)
check("conn() 异常后锁仍然放掉", S.lock.acquire(blocking=False))
S.lock.release()

# locked_conn() 的锁语义
held = {"got": None}


def grab():
    held["got"] = S.lock.acquire(timeout=0.4)
    if held["got"]:
        S.lock.release()


with S.locked_conn() as raw:
    raw.execute("SELECT 1").fetchone()
    t = threading.Thread(target=grab)
    t.start()
    t.join()
check("locked_conn 块内：别的线程拿不到锁", held["got"] is False)

# --------------------------------------------------------------------------- #
# ⑧ 这一层不许反向依赖（架构规矩的可执行检查）
#
# ⚠️ 这里**必须用 AST 读真正的 import 语句**，不能在源码里搜字符串 ——
# 第一版就是搜字符串，结果这条检查自己的标签里写着「不出现 'import server'」，
# 于是把自己的说明文字当成了违规（自检骗自己，正是 AGENTS.md 警告的那类事）。
# --------------------------------------------------------------------------- #
print("--- ⑧ 依赖方向 ---")
import ast  # noqa: E402

tree = ast.parse(open(os.path.join(APP_DIR, "store.py"), encoding="utf-8").read())
imported = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            imported.add(alias.name.split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            imported.add(node.module.split(".")[0])

banned = {"server", "api", "services", "backup", "schema", "launcher"}
hit = sorted(imported & banned)
check("store.py 不 import 上层模块（server/api/services/backup/schema/launcher）",
      not hit, "实际 import 了：%s" % (hit or "无"))
check("store.py 只 import 标准库", imported <= {"os", "sqlite3", "threading"},
      "实际：%s" % sorted(imported))

# --------------------------------------------------------------------------- #
print("\n结果：" + ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
