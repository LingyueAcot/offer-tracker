# -*- coding: utf-8 -*-
"""
services/notes.py —— 备忘录：读 / 新增 / 改 / 删

2026-09-24 从 `server.py` 搬过来（S4 的第一个域，选它当试点是因为最小、最好验）。
搬的时候**只改了 import 的来源，业务逻辑一行没动**：原来的 `store.db().locked_cursor()`
照旧，事务边界照旧（写完提交，再记审计）。

为什么搬：原来这四个函数在 `server.py` 里，`api/notes.py` 只能
`from server import create_note, ...` —— 那就是 `api/ → server` 的反向依赖。
搬进 `services/` 之后，`api/` 依赖的是这一层，方向就正过来了。

【规矩】
- 不 import `server`（靠 `store` / `core` / `services.audit`）。
- 返回值的形状**和原来一模一样**（`api/` 与自检都按那个形状用）：
    list_notes(cur=None) -> [dict, ...]
    create_note(payload) -> (note_id, None) 或 (None, "错误信息")
    update_note(id, payload) -> None 或 "错误信息"
    delete_note(id) -> None
- `store` 用**模块级** import（不叫 `store` 是免得和局部变量撞名）。
  自检要换库时改它的 `db_path` 即可，不必往每个函数里塞参数。
"""

import store as store_module
from core.values import as_int, clean, now_str

from services.audit import log_change


def list_notes(cur=None):
    """
    读备忘录：没完成的排前面，完成的沉到后面；同组按登记时间倒序（新记的在上）。

    `cur` 是**给调用方传进来的**：`build_state()` 已经在它自己的事务里拿了 cursor，
    传进来就能共用同一条连接（不传的话这里会自己借一条 —— 2026-09-24 之前
    `build_state()` 就是这么多开了第二条连接）。
    """
    sql = "SELECT * FROM notes ORDER BY done ASC, sort_order ASC, id DESC"
    if cur is not None:
        return [dict(row) for row in cur.execute(sql)]

    # 没人传 cursor：自己借一条（store 保证出块时提交并归还）
    with store_module.db().locked_cursor() as own_cur:
        return [dict(row) for row in own_cur.execute(sql)]


def create_note(payload):
    """新增一条备忘录。只要求一句话，其它都给默认值。"""
    text = clean(payload.get("text"), 500)
    if not text:
        return None, "备忘录内容不能为空"
    stamp = now_str()
    # 事务边界跟原来一模一样：先写完并提交，再记改动日志（日志在事务外）。
    with store_module.db().locked_cursor() as cur:
        cur.execute(
            """
            INSERT INTO notes (text, done, sort_order, created_at, updated_at)
            VALUES (?, 0, ?, ?, ?)
            """,
            (text, as_int(payload.get("sort_order")) or 500, stamp, stamp),
        )
        note_id = cur.lastrowid
    log_change("create", "note", note_id, "新增备忘录：%s" % text, before=None,
               after={"id": note_id, "text": text})
    return note_id, None


def update_note(note_id, payload):
    """改一句话，或者打勾 / 取消打勾。只传其中一个也行。"""
    with store_module.db().locked_cursor() as cur:
        before = cur.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if not before:
            return "备忘录不存在"
        before = dict(before)
        fields, values = [], []
        if "text" in payload:
            fields.append("text=?")
            values.append(clean(payload.get("text"), 500))
        if "done" in payload:
            fields.append("done=?")
            values.append(1 if payload.get("done") else 0)
        if "sort_order" in payload:
            fields.append("sort_order=?")
            values.append(as_int(payload.get("sort_order")) or 500)
        if not fields:
            return None          # 没传任何可改字段：空事务，直接走（store 照常提交/归还）
        fields.append("updated_at=?")
        values.append(now_str())
        values.append(note_id)
        cur.execute("UPDATE notes SET %s WHERE id=?" % ", ".join(fields), tuple(values))
        # 事务边界与原来一致：先把 before/after 都读出来（都在这条连接里），
        # 出块提交之后才写改动日志。
        after = dict(cur.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone())
    log_change("update", "note", note_id, "修改备忘录：%s" % (after.get("text") or ""),
               before=before, after=after)
    return None


def delete_note(note_id):
    """删掉一条备忘录，删除前的内容照样进改动日志。"""
    with store_module.db().locked_cursor() as cur:
        before = cur.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        cur.execute("DELETE FROM notes WHERE id=?", (note_id,))
        # 原来这里是「先 commit 再 log_change」；改动日志不能因为后面出意外而丢，
        # 所以照旧：先把删除提交掉（出块时 store 提交），再写日志。
    if before:
        log_change("delete", "note", note_id, "删除备忘录：%s" % dict(before).get("text"),
                   before=dict(before), after=None)
    return None
