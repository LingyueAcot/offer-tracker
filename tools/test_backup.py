# -*- coding: utf-8 -*-
"""
启动备份（app/backup.py）的回归测试 —— 纯逻辑，不用开程序，也不碰真实数据。

用法：
    python tools\test_backup.py             # 在系统临时目录里造试验场
    python tools\test_backup.py <空目录>     # 指定试验场（受限环境里 mkdtemp 建不出子目录时用这个）

【要守住的是什么】
「每次启动都留一份能用的备份」。所以这里造一个假 app\\data\\，把几种情形都过一遍：

  1. 还没有数据（首次运行）      -> 不报错、不留空文件夹
  2. 正常数据                    -> 建出库 / 改动日志 / 画像 / manifest 齐全的一份
  3. 改动日志最后一行写了一半    -> 只拷完整的行（半行丢掉）
  4. 同一秒再跑一次              -> 新建一份，不覆盖上一份
  5. 库文件是坏的                -> 返回 None、不留半成品文件夹、日志里有记录
  6. latest_backup()             -> 认得出最新那一份

最后一条是这次特意加的：**宁可没有备份，也不能留一个看起来能用、其实坏掉的备份文件夹**
（那种东西会在真的需要恢复时骗人）。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP_DIR)
import backup  # noqa: E402

ok = True


def check(label, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("[PASS] " if cond else "[FAIL] ") + label + ("  " + extra if extra else ""))


def make_app_dir(root, with_db=True, corrupt_db=False, rows=3):
    """造一个假程序目录：<root>\\app\\ 下面放 data\\ 和个人画像.json"""
    base = os.path.join(root, "app")
    data = os.path.join(base, "data")
    os.makedirs(data)
    if with_db:
        db = os.path.join(data, "applications.db")
        if corrupt_db:
            with open(db, "wb") as handle:
                handle.write(b"definitely not a sqlite file" * 20)
        else:
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY, company TEXT)")
            for index in range(rows):
                conn.execute("INSERT INTO applications(company) VALUES(?)", ("公司%d" % index,))
            conn.commit()
            conn.close()
    # 改动日志：故意让最后一行写一半（模拟程序正开着）
    with open(os.path.join(data, "change_log.jsonl"), "wb") as handle:
        handle.write('{"a":1}\n{"a":2}\n{"half'.encode("utf-8"))
    with open(os.path.join(base, "个人画像.json"), "w", encoding="utf-8") as handle:
        json.dump({"major": "材料"}, handle, ensure_ascii=False)
    return base


def backup_dirs(base):
    parent = os.path.join(base, "data", backup.BACKUP_DIRNAME)
    if not os.path.isdir(parent):
        return []
    return sorted(name for name in os.listdir(parent)
                  if os.path.isdir(os.path.join(parent, name)))


scratch = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="backup_test_")
if not os.path.isdir(scratch):
    os.makedirs(scratch)
print("试验场：%s\n" % scratch)
root = os.path.join(scratch, "case1")
os.makedirs(root)

# ---------- 1) 首次运行：还没有数据 ----------
base = make_app_dir(root, with_db=False)
info = backup.run_startup_backup(base, os.path.join(base, "logs", "backup.log"))
check("首次运行（没有库）不报错、不备份", info is None)
check("首次运行不留空文件夹", backup_dirs(base) == [])

# ---------- 2) 正常数据 ----------
root2 = os.path.join(scratch, "case2")
os.makedirs(root2)
base2 = make_app_dir(root2, rows=3)
log2 = os.path.join(base2, "logs", "backup.log")
info = backup.run_startup_backup(base2, log2)
check("正常数据会备份", bool(info))
if info:
    check("备份文件夹存在", os.path.isdir(info["dir"]), info.get("dir", ""))
    check("manifest 记了行数", info.get("db_rows") == 3, "db_rows=%s" % info.get("db_rows"))
    copy_db = os.path.join(info["dir"], "applications.db")
    check("副本能打开", os.path.isfile(copy_db))
    conn = sqlite3.connect(copy_db)
    count = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    check("副本里行数一致", count == 3, "副本 %d 行" % count)
    conn.close()
    with open(os.path.join(info["dir"], "change_log.jsonl"), encoding="utf-8") as handle:
        kept = handle.read()
    check("改动日志只拷完整行（半行丢掉）", kept == '{"a":1}\n{"a":2}\n', repr(kept))
    check("个人画像一起备份了", os.path.isfile(os.path.join(info["dir"], "个人画像.json")))
    check("manifest.json 存在且可解析",
          isinstance(json.load(open(os.path.join(info["dir"], "manifest.json"), encoding="utf-8")), dict))
    check("没有 .tmp- 残留", [n for n in backup_dirs(base2) if n.startswith(".tmp")] == [])

# ---------- 3) 同一秒再跑一次：新建一份不覆盖 ----------
info2 = backup.run_startup_backup(base2, log2)
dirs2 = backup_dirs(base2)
check("再跑一次会多一份", len(dirs2) == 2, "共 %d 份：%s" % (len(dirs2), dirs2))
check("两份不是同一个文件夹", bool(info2) and info2["dir"] != info["dir"])
check("第一份还在（没被覆盖）", bool(info) and os.path.isdir(info["dir"]))

# ---------- 4) latest_backup() ----------
latest = backup.latest_backup(base2)
check("latest_backup 认得出最新那份",
      bool(latest) and os.path.normcase(latest["dir"]) == os.path.normcase(info2["dir"]),
      "拿到 %s" % (latest or {}).get("dir", "None"))

# ---------- 5) 坏库：不留半成品 ----------
root3 = os.path.join(scratch, "case5")
os.makedirs(root3)
base3 = make_app_dir(root3, corrupt_db=True)
log3 = os.path.join(base3, "logs", "backup.log")
info3 = backup.run_startup_backup(base3, log3)
check("坏库不会抛异常出来", info3 is None)
check("坏库不留半成品文件夹", backup_dirs(base3) == [], str(backup_dirs(base3)))
check("坏库不留 .tmp- 残留",
      not [n for n in os.listdir(os.path.join(base3, "data", backup.BACKUP_DIRNAME))
           if n.startswith(".tmp")])
with open(log3, encoding="utf-8") as handle:
    logged = handle.read()
check("失败原因写进日志了", "启动备份失败" in logged)

# ---------- 6) 真实数据没被动过 ----------
check("原库还在原处、行数没变", os.path.isfile(os.path.join(base2, "data", "applications.db")))
conn = sqlite3.connect(os.path.join(base2, "data", "applications.db"))
check("原库仍是 3 行", conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 3)
conn.close()

shutil.rmtree(scratch, ignore_errors=True)
print("\n结果：" + ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
