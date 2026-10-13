# -*- coding: utf-8 -*-
"""
services/audit.py —— 改动日志（change_log.jsonl）的读写

【为什么先把它单独拆出来】
`log_change()` 原来在 `server.py` 里。而「写一条备忘录」「改一条投递」这些**业务动作**
都要调它记审计 —— 所以一旦把这些业务动作搬进 `services/`，它们就得回头 import `server`，
那又变成反向依赖了。

把审计这一小块先搬出来（它只依赖「日志文件路径」和「一把文件锁」，跟业务无关），
`services/` 里的各域就能直接用，不必回头找 `server`。
`server.py` 再把这两个名字**转发**出来（见那边的 import 注释），行为一字不变。

【规矩】
- 这里只管「往 change_log.jsonl 追加/读取」。**不碰业务数据库**（那是 store.py 的事）。
- 日志路径由 `server.py` 在启动时用 `use()` 注入 —— 路径的唯一定义处仍然在 `server.py`
  顶部（`CHANGELOG_PATH`），不在第二个地方重算。
- 写日志**永远不许把主流程弄崩**：所以两边都是 try/except 吞掉（这是**原有行为**，
  注释说明为什么，不是随手写的空 except：审计失败可以接受，业务操作失败不能）。
"""

import json
import os
import threading

# 日志文件路径。server.py 启动时注入。
_changelog_path = {"value": ""}

# 文件锁：其实现在只有主进程在写，但读和写要互斥（读的时候可能正写到一半）。
# 原来 server.py 里叫 LOG_LOCK，语义一字不动搬过来。
LOG_LOCK = threading.RLock()


def use(changelog_path):
    """server.py 启动时调一次，把日志路径装进来。"""
    _changelog_path["value"] = changelog_path


def path():
    """当前日志路径（自检脚本会读它做断言）。"""
    return _changelog_path["value"]


def log_change(action, entity, entity_id, summary, before=None, after=None,
               now_str=None):
    """
    把每一次改动追加到独立的 change_log.jsonl，包括删除前的完整数据。

    `now_str` 是注入进来的时间函数（默认用 core.values.now_str）——
    这样 `services/` 不必依赖 `server` 拿时间戳，也方便测试固定时间。
    """
    if now_str is None:
        from core.values import now_str as _now
        now_str = _now
    entry = {
        "time": now_str(),
        "action": action,
        "entity": entity,
        "id": entity_id,
        "summary": summary,
        "before": before,
        "after": after,
    }
    target = _changelog_path["value"]
    if not target:
        return
    with LOG_LOCK:
        try:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:
            # 审计写失败不能把业务弄崩（原行为就是这样）
            pass


def read_changelog(limit=800):
    """读最近的改动记录，最新的在前。"""
    entries = []
    target = _changelog_path["value"]
    if not target or not os.path.isfile(target):
        return entries
    with LOG_LOCK:
        try:
            with open(target, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except Exception:
            return entries
    entries.reverse()
    return entries[:limit]
