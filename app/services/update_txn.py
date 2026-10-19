# -*- coding: utf-8 -*-
"""启动前只读检查升级事务；恢复由独立更新器在退出/持锁后执行。

目录契约与 tools/updater.py:txn_dir 一致，由离线门逐装法比较。
不能在这里回滚：启动进程正在使用程序文件，更新器才有退出等待与独占锁。
"""
import json
import os

TXN_NAME = "update-txn.json"
GUIDANCE = "上次升级没做完，请重新运行 `一键升级.cmd` 或联系作者；你的数据没被动。"


def txn_dirs(target):
    """**两个候选事务目录，顺序＝本地优先**。

    ⚠️ 为什么要有这个函数（2026-10-12 Astra 复验指出的漏检）：
    更新器**写**事务时只挑一个目录（本地可写就写本地，否则落 `%LOCALAPPDATA%`），
    但**检查**必须两个都看 —— 否则"上次升级把事务落在了 A 目录、这次本地又变成可写"时，
    检查只看 B 目录 → 认为没有事务 → 带着半新半旧的程序启动。
    所以：写入按 `txn_dir()` 挑一个，**检查按这里逐个看**。
    """
    local = os.path.join(os.path.abspath(target), "更新数据")
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    external = os.path.join(base, "ResumeTracker", "更新数据")
    out = []
    for item in (local, external):
        if os.path.normcase(item) not in [os.path.normcase(x) for x in out]:
            out.append(item)
    return out


def txn_dir(target):
    """只算目录，不创建：已存在的本地事务目录永不因不可写而被隐藏。"""
    local, external = txn_dirs(target)
    ancestor = local
    while not os.path.exists(ancestor):
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            break
        ancestor = parent
    if os.path.lexists(local) or os.access(ancestor, os.W_OK):
        return local
    return external


def _check_one(path, target):
    """查一个目录里的事务。返回 (允许启动, 原因)；这个目录里没有问题就返回 (True, "")。"""
    try:
        with open(path, encoding="utf-8") as handle:
            txn = json.load(handle)
    except FileNotFoundError:
        return True, ""
    except (OSError, ValueError) as err:
        return False, "%s\n事务记录无法可靠读取：%s（%s）" % (GUIDANCE, path, err)
    if (not isinstance(txn, dict) or not isinstance(txn.get("target"), str)
            or os.path.normcase(os.path.abspath(txn["target"])) != os.path.normcase(target)):
        return False, "%s\n事务记录损坏或安装目录不符：%s" % (GUIDANCE, path)
    phase = txn.get("phase")
    applied = txn.get("applied", [])
    if phase in ("preflight", "wait-exit", "backup") and applied == []:
        # 文案里必须留「尚未替换」：日志与自检都靠它判断"这次是安全的"（别删）
        return True, "上次升级尚未替换程序文件（阶段=%s），可安全继续；事务保留供更新器恢复。" % phase
    if phase in ("done", "rolled-back"):
        return True, "上次升级事务已完成（阶段=%s）；事务保留供更新器核对。" % phase
    return False, "%s\n拒绝在可能半新半旧的程序上启动（阶段=%s，事务=%s）。" % (GUIDANCE, phase, path)


def check_startup(program):
    """返回 (允许启动, 人话原因)；不清事务、不读数据库、不写安装树。

    **两个候选目录都要查**（见 `txn_dirs()` 的说明）：任何一个里有"已进入替换阶段"的事务就拒绝；
    两个都安全时，把第一个非空的说明带出去（例如「尚未替换程序文件…可安全继续」）。
    """
    target = os.path.dirname(os.path.abspath(program))
    checked, notes = [], []
    for base in txn_dirs(target):
        allowed, reason = _check_one(os.path.join(base, TXN_NAME), target)
        if not allowed:
            return False, reason
        if reason:
            notes.append(reason)
        checked.append(base)
    if notes:
        return True, notes[0]
    return True, "没有未完成升级事务（查过：%s）。" % "；".join(checked)
