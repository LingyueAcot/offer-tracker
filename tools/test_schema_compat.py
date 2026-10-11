# -*- coding: utf-8 -*-
r"""数据库兼容判据（S/R/M）的自检：纯逻辑 + 真库往返。

为什么单独一个门：以前判"这库能不能开"是拿**两个程序常量**比，被 外部评审 复审推翻
（库里的版本记录因为 `INSERT OR IGNORE` 从不更新，比常量既会拦正常迁移、又会放过旧程序开新库）。
现在有两件东西必须一起对：① `services/schema_compat.check()` 的判据；② `schema.py` 真的把
**库里那条记录**迁移成新值（否则判据永远看不到真相）。

用法：python tools\test_schema_compat.py
"""
from __future__ import print_function

import os
import sqlite3
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))

from services import schema_compat as C      # noqa: E402

ok = True
passed = failed = 0


def check(label, cond, extra=""):
    global ok, passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        ok = False
    print(("  [PASS] " if cond else "  [FAIL] ") + label + ("  " + extra if extra else ""))


def main():
    print("== 一、判据本身（纯逻辑）==")
    cases = [
        ("当前版本（2）直接兼容", 2, True),
        ("老版本（1）有迁移路径", 1, True),
        ("没有记录（很老的库/全新库）当可迁移", None, True),
        ("更高的版本（3，更新版程序建的库）必须拒绝", 3, False),
        ("完全不认识的值（7）必须拒绝", 7, False),
        ("版本记录不是数字必须拒绝", "abc", False),
        ("版本记录是 0 必须拒绝", 0, False),
    ]
    for label, current, want in cases:
        got, reason = C.check(current)
        check(label, got is want, reason)

    print("\n== 二、真库往返：迁移后库里的记录必须变成真实版本 ==")
    scratch = tempfile.mkdtemp(prefix="schema-compat-")
    try:
        db_path = os.path.join(scratch, "applications.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row      # ensure_schema 按列名取值（store 里也是这么配的）
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        # 造一个"老库"：记录写着 1，但结构已经是 2（模拟"迁移过、记录没更新"的历史状态）
        conn.execute("INSERT INTO meta(key,value) VALUES('schema_version','1')")
        conn.commit()

        import schema
        schema.ensure_schema(conn, [], 2, "9.9.9")
        conn.commit()
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        check("迁移后库里的记录被更新成 2（UPSERT 生效，不再是 OR IGNORE 的旧值）",
              row and row[0] == "2", "实际 %r" % (row[0] if row else None))

        # 再跑一次：幂等，仍然是 2
        schema.ensure_schema(conn, [], 2, "9.9.9")
        conn.commit()
        row2 = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        check("重复迁移幂等（仍是 2）", row2 and row2[0] == "2")
        conn.close()

        # 用 store 的读法读一遍（判据实际用的就是这个读者）
        import store
        store.use(db_path)
        check("store.read_schema_version() 读出 2", store.read_schema_version() == 2,
              "实际 %r" % store.read_schema_version())

        # 全新库（还没有 meta 表）→ 读不到，判据放行（会走建库）
        fresh = os.path.join(scratch, "fresh.db")
        sqlite3.connect(fresh).close()
        store.use(fresh)
        check("全新库读不到版本记录", store.read_schema_version() is None)
        check("全新库判据放行", C.check(store.read_schema_version())[0] is True)

        # ★ 真正的全新安装：**库文件还不存在、父目录也没建** → 必须放行，绝不许报错。
        #   2026-10-12 发布包首启自检逮到的真 bug：这里报错会让"全新安装永远起不来"。
        brand_new = os.path.join(scratch, "还没建的目录", "applications.db")
        store.use(brand_new)
        check("库文件不存在（全新安装）：读版本得到 None 且不抛",
              store.read_schema_version() is None)
        check("全新安装判据放行（不会拒绝启动）", C.check(store.read_schema_version())[0] is True)
    finally:
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)

    print("\n共 %d 项：通过 %d · 失败 %d" % (passed + failed, passed, failed))
    print("结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
