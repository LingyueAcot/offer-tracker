# -*- coding: utf-8 -*-
"""
启动时自动备份数据 —— 每次启动都留一份，不覆盖、不自动清理。

【为什么要它】
data/applications.db 是唯一一份真实数据（投递 / 进展 / 宣讲会 / 备忘录），
data/change_log.jsonl 是完整改动历史。改代码、跑自检、手滑误操作都可能弄坏它们，
所以不指望「用户记得手动备份」。

server.py 的 main() 在 init_db() **之前**调它 —— 万一哪天迁移把库改坏，
手里至少还有迁移前那一刻的版本。

【备份什么、怎么备】
  applications.db   → 走 SQLite 自己的在线备份接口（Connection.backup），
                      **不是 shutil.copy**：程序可能正开着，直接拷文件可能拿到半截事务。
  change_log.jsonl  → 按行复制，**丢掉可能写了一半的最后一行**。
  个人画像.json      → 有就一起拷（用户自己维护的对口依据）。
不备份 config.json（那是设置，跟着程序走），也不备份已有的 research_backup_*.json
（它们本身就是备份，再套一层没意义）。

【落到哪】
  data/backups/<YYYY-MM-DD_HHMMSS>/
      applications.db     主数据的副本（一致性快照）
      change_log.jsonl    改动日志的副本
      个人画像.json        有才拷
      manifest.json       这一份是什么时候、从哪、多大、校验过没有

  每次启动新建一个文件夹；同一秒启动两次会自动加 -2、-3 后缀。
  **不自动清理**（用户明确要求每次都留）。想清就整个 data/backups/ 删掉，没有副作用。

【硬约定】
  - 备份失败**绝不能拦住程序启动**：调 run_startup_backup() 拿 None 就继续跑。
  - 先写到 .tmp-<时间戳>/ 再改名，所以**磁盘上不会出现半成品备份文件夹**：
    失败就把临时目录删掉，宁可没有备份，也不留一个看起来能用、其实坏的。
  - 复制完立刻 PRAGMA quick_check + 数一遍行数；不过关按失败处理。
  - 只用标准库（这个项目不许引入依赖）。
"""
from __future__ import print_function

import json
import os
import shutil
import sqlite3
import time
import traceback

BACKUP_DIRNAME = "backups"
DB_NAME = "applications.db"
CHANGELOG_NAME = "change_log.jsonl"
PROFILE_NAME = "个人画像.json"
MANIFEST_NAME = "manifest.json"
TMP_PREFIX = ".tmp-"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _log(log_path, message):
    """写一行备份日志（默认 logs/backup.log）。写不进去也不能影响备份本身。"""
    if not log_path:
        return
    try:
        parent = os.path.dirname(log_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


def _free_name(parent, base):
    """找一个还不存在的名字（同一秒跑两次也不会互相覆盖）。"""
    target = os.path.join(parent, base)
    index = 2
    while os.path.exists(target):
        target = os.path.join(parent, "%s-%d" % (base, index))
        index += 1
    return target


def _copy_sqlite(src, dst):
    """用 SQLite 在线备份接口复制主库，返回 (applications 行数, quick_check 是否 ok)。"""
    source = sqlite3.connect(src, timeout=20)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    check = sqlite3.connect(dst)
    try:
        row = check.execute("PRAGMA quick_check").fetchone()
        ok = bool(row) and row[0] == "ok"
        rows = check.execute("SELECT COUNT(*) FROM applications").fetchone()[0] if ok else -1
    finally:
        check.close()
    return rows, ok


def _copy_complete_lines(src, dst):
    """
    按行复制文本，丢掉可能写了一半的最后一行（日志是边跑边追加的）。
    返回复制过去的完整行数。
    """
    with open(src, "rb") as handle:
        raw = handle.read()
    if raw and not raw.endswith(b"\n"):
        cut = raw.rfind(b"\n")
        raw = raw[: cut + 1] if cut >= 0 else b""
    with open(dst, "wb") as handle:
        handle.write(raw)
    return raw.count(b"\n")


# --------------------------------------------------------------------------- #
# 对外：跑一次启动备份
# --------------------------------------------------------------------------- #
def run_startup_backup(base_dir, log_path=None, data_dir=None, profile_path=None):
    """
    备份一次。返回 info 字典；没有数据可备（首次运行）或失败时返回 None。

    **不抛异常** —— 调用方（server.py 的 main()）直接拿着返回值继续启动。

    路径（2026-10-11 加的两个参数）：正式启动时由 `server.py` 显式传 `data_dir` / `profile_path`
    （它们的唯一决定处是 `app/paths.py`）；自检脚本按老办法只给 `base_dir` 也能用（这时就按
    "数据在程序隔壁"推）—— 保持向后兼容，别让 `tools/test_backup.py` 这种老调用方失效。
    """
    if data_dir is None:
        data_dir = os.path.join(base_dir, "data")
    db_path = os.path.join(data_dir, DB_NAME)
    changelog_path = os.path.join(data_dir, CHANGELOG_NAME)
    if profile_path is None:
        profile_path = os.path.join(base_dir, PROFILE_NAME)

    if not os.path.isfile(db_path):
        _log(log_path, "启动备份：还没有 %s，跳过（首次运行？）" % DB_NAME)
        return None

    parent = os.path.join(data_dir, BACKUP_DIRNAME)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    tmp_dir = None
    try:
        if not os.path.isdir(parent):
            os.makedirs(parent)
        tmp_dir = _free_name(parent, TMP_PREFIX + stamp)
        os.makedirs(tmp_dir)

        db_bytes = os.path.getsize(db_path)
        rows, ok = _copy_sqlite(db_path, os.path.join(tmp_dir, DB_NAME))
        if not ok:
            raise RuntimeError("复制出来的库没通过 PRAGMA quick_check")

        info = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "stamp": stamp,
            "reason": "startup",
            "db_bytes": db_bytes,
            "db_rows": rows,
            "changelog_bytes": 0,
            "changelog_lines": 0,
            "profile": False,
            "ok": True,
        }

        if os.path.isfile(changelog_path):
            info["changelog_bytes"] = os.path.getsize(changelog_path)
            info["changelog_lines"] = _copy_complete_lines(
                changelog_path, os.path.join(tmp_dir, CHANGELOG_NAME)
            )
        if os.path.isfile(profile_path):
            with open(profile_path, "rb") as src_handle:
                with open(os.path.join(tmp_dir, PROFILE_NAME), "wb") as dst_handle:
                    dst_handle.write(src_handle.read())
            info["profile"] = True

        final_dir = _free_name(parent, stamp)
        info["dir"] = final_dir
        info["source_dir"] = base_dir
        with open(os.path.join(tmp_dir, MANIFEST_NAME), "w", encoding="utf-8") as handle:
            json.dump(info, handle, ensure_ascii=False, indent=2)

        os.rename(tmp_dir, final_dir)   # 到这一步才算数：磁盘上不会留半成品
        tmp_dir = None
        _log(
            log_path,
            "启动备份完成：%s（库 %d 字节 / %d 条，改动日志 %d 行）"
            % (final_dir, info["db_bytes"], info["db_rows"], info["changelog_lines"]),
        )
        return info
    except Exception:
        _log(log_path, "启动备份失败（不影响程序启动）：\n" + traceback.format_exc())
        if tmp_dir:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
        return None


def latest_backup(base_dir):
    """读 data/backups/ 里最新那一份的 manifest.json；没有就返回 None。"""
    parent = os.path.join(base_dir, "data", BACKUP_DIRNAME)
    if not os.path.isdir(parent):
        return None
    names = sorted(
        name for name in os.listdir(parent)
        if not name.startswith(TMP_PREFIX) and os.path.isdir(os.path.join(parent, name))
    )
    for name in reversed(names):
        manifest = os.path.join(parent, name, MANIFEST_NAME)
        try:
            with open(manifest, "r", encoding="utf-8") as handle:
                info = json.load(handle)
            if isinstance(info, dict):
                info.setdefault("dir", os.path.join(parent, name))
                return info
        except Exception:
            continue
    return None
