# -*- coding: utf-8 -*-
"""
services/resumes.py —— 简历：读 / 新增 / 保存 / 删除 / 复制 / 设为默认

【为什么 JSON 编解码在这一层，不在 core】
`tools/test_architecture.py` 的 **R3** 明令 `core/` 不许 import `json` / `os` / `io` / `pathlib`
（那些都是间接碰文件的口子）。所以 `core/resume.py` 只做纯逻辑（默认值、校验、id 生成），
**把 dict ↔ 字符串这件事放在这里**。

【规矩】（照 `services/notes.py` 那个最小域来）
- 不 import `server`（靠 `store` / `core` / `services.audit`）。
- SQL 由本层用 `store` 的 cursor 写（R5 只约束 `sqlite3.connect` 不许出 store）。
- 写操作一律 `log_change(...)`；**事务内写完、出块提交后再记审计**（与既有各域一致）。
- 保存用 `rev` 做**乐观并发**：对不上就冲突返回，**一个字都不写**。
- 库里存的是 `content` / `layout` 两段 JSON 文本，**正文与排版分开**。
- 坏 JSON 不让请求 500：解析失败退回默认结构（手工改坏库的兜底）。

【返回值形状】
    list_resumes(cur=None)            -> [dict, ...]（只含元信息，不含 content）
    get_resume(id, cur=None)          -> dict 或 None
    create_resume(payload)            -> (id, None) 或 (None, "错误")
    save_resume(id, payload)          -> (None, None) 成功
                                         ("错误", None) 校验/不存在
                                         (**None**, {"rev":n,"updated_at":s}) 并发冲突
    delete_resume(id)                 -> None 或 "错误"
    duplicate_resume(id)              -> (id, None) 或 (None, "错误")
    set_default_resume(id)            -> None 或 "错误"
"""

import json

import store as store_module
from core.resume import (CONTENT_VER, LAYOUT_VER, MAX_TITLE, default_content,
                         default_layout, deep_clone, normalize_content,
                         normalize_layout, custom_section_error)
from core.values import as_int, clean, now_str

from services.audit import log_change


# --------------------------------------------------------------------------- #
# JSON 编解码（只在服务层做）
# --------------------------------------------------------------------------- #
def _dump(value):
    """序列化。sort_keys 是为了让同样的内容产出同样的字符串，方便比对/测试。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None          # 坏行当作没有，不让整个请求崩掉


def _read_content(text):
    """把库里的 content 文本读成规范结构；坏数据退回默认而不是 500。"""
    raw = _load(text)
    if raw is None:
        return default_content()
    content, error = normalize_content(raw)
    if error is None:
        return content
    return raw if isinstance(raw, dict) else default_content()


def _read_layout(text):
    raw = _load(text)
    if raw is None:
        return default_layout()
    layout, error = normalize_layout(raw)
    if error is None:
        return layout
    return raw if isinstance(raw, dict) else default_layout()


def _meta(row):
    """列表用的轻量行：**不带 content/layout**，省得每次刷新都传几十 KB。"""
    item = dict(row)
    return {
        "id": item.get("id"),
        "title": item.get("title") or "",
        "is_default": 1 if item.get("is_default") else 0,
        "sort_order": item.get("sort_order") or 500,
        "content_ver": item.get("content_ver") or CONTENT_VER,
        "rev": item.get("rev") or 1,
        "created_at": item.get("created_at") or "",
        "updated_at": item.get("updated_at") or "",
    }


def _full(row):
    item = dict(row)
    item["content"] = _read_content(item.get("content"))
    item["layout"] = _read_layout(item.get("layout"))
    item["is_default"] = 1 if item.get("is_default") else 0
    item["content_ver"] = item.get("content_ver") or CONTENT_VER
    item["rev"] = item.get("rev") or 1
    return item


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def list_resumes(cur=None):
    """
    读简历列表（只读元信息）。默认那份排最前，其余按 sort_order、id。

    `cur` 与 `services.notes.list_notes` 同一个约定：`build_state()` 会传自己的 cursor 进来，
    这样一次请求只用一条连接。
    """
    sql = "SELECT * FROM resumes ORDER BY is_default DESC, sort_order ASC, id ASC"
    if cur is not None:
        return [_meta(row) for row in cur.execute(sql)]
    with store_module.db().locked_cursor() as own_cur:
        return [_meta(row) for row in own_cur.execute(sql)]


def get_resume(resume_id, cur=None):
    """读一份完整简历（含 content / layout）。没有就返回 None。"""
    sql = "SELECT * FROM resumes WHERE id=?"
    if cur is not None:
        row = cur.execute(sql, (resume_id,)).fetchone()
        return _full(row) if row else None
    with store_module.db().locked_cursor() as own_cur:
        row = own_cur.execute(sql, (resume_id,)).fetchone()
        return _full(row) if row else None


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
def create_resume(payload):
    """
    新建一份简历。没传 content/layout 就给默认结构。

    第一份自动成为默认（免得"一份都没有默认"这种状态）。
    """
    payload = payload if isinstance(payload, dict) else {}

    title = clean(payload.get("title"), MAX_TITLE) or "未命名简历"

    # 自定义板块超限就**不写库**（外部评审方案：超限报错，不截断、不静默丢）
    cap_error = custom_section_error(payload.get("content"))
    if cap_error:
        return None, cap_error

    content = default_content()
    if payload.get("content") is not None:
        content, error = normalize_content(payload.get("content"))
        if error:
            return None, error
    layout = default_layout()
    if payload.get("layout") is not None:
        layout, error = normalize_layout(payload.get("layout"))
        if error:
            return None, error

    stamp = now_str()
    with store_module.db().locked_cursor() as cur:
        total = cur.execute("SELECT COUNT(*) AS n FROM resumes").fetchone()["n"]
        is_default = 1 if total == 0 else 0
        cur.execute(
            """
            INSERT INTO resumes (title, is_default, sort_order, content, layout,
                                 content_ver, rev, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (title, is_default, as_int(payload.get("sort_order")) or 500,
             _dump(content), _dump(layout), CONTENT_VER, stamp, stamp),
        )
        resume_id = cur.lastrowid
    log_change("create", "resume", resume_id, "新增简历：%s" % title, before=None,
               after={"id": resume_id, "title": title, "is_default": is_default,
                      "content_ver": CONTENT_VER})
    return resume_id, None


def save_resume(resume_id, payload):
    """
    保存一份简历（正文 / 排版 / 标题，只传要改的）。

    **乐观并发**：payload 必须带 `rev`；与服务端不一致时返回冲突信息、不写库。
    """
    payload = payload if isinstance(payload, dict) else {}

    if "rev" not in payload:
        return "缺少 rev（保存需要带上读取时的版本号）", None
    client_rev = as_int(payload.get("rev"))
    if client_rev is None:
        return "rev 必须是整数", None

    content_text = None
    layout_text = None
    if payload.get("content") is not None:
        content, error = normalize_content(payload.get("content"))
        if error:
            return error, None
        content_text = _dump(content)
    if payload.get("layout") is not None:
        layout, error = normalize_layout(payload.get("layout"))
        if error:
            return error, None
        layout_text = _dump(layout)
    title = None
    if payload.get("title") is not None:
        title = clean(payload.get("title"), MAX_TITLE) or "未命名简历"

    with store_module.db().locked_cursor() as cur:
        before_row = cur.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
        if not before_row:
            return "简历不存在", None
        before = dict(before_row)

        # —— 并发冲突：一个字都不写 ——
        if (before.get("rev") or 1) != client_rev:
            return None, {"rev": before.get("rev") or 1,
                          "updated_at": before.get("updated_at") or ""}

        fields, values = [], []
        if content_text is not None:
            fields.append("content=?")
            values.append(content_text)
        if layout_text is not None:
            fields.append("layout=?")
            values.append(layout_text)
        if title is not None:
            fields.append("title=?")
            values.append(title)
        if not fields:
            return None, None            # 没传可改字段：空事务，直接算成功

        fields.append("content_ver=?")
        values.append(CONTENT_VER)
        fields.append("rev=rev+1")
        fields.append("updated_at=?")
        values.append(now_str())
        values.append(resume_id)
        cur.execute("UPDATE resumes SET %s WHERE id=?" % ", ".join(fields), tuple(values))
        after = dict(cur.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone())

    log_change("update", "resume", resume_id, "保存简历：%s" % (after.get("title") or ""),
               before={"id": before["id"], "title": before.get("title"),
                       "rev": before.get("rev"), "updated_at": before.get("updated_at")},
               after={"id": after["id"], "title": after.get("title"),
                      "rev": after.get("rev"), "updated_at": after.get("updated_at")})
    return None, None


def _audit_row(row):
    """
    审计用的"删除前完整内容"：**证件照的 base64 不进审计**。

    为什么：一张证件照的 base64 约 34 万字符，直接写进 change_log.jsonl 会让日志和启动备份
    都膨胀；审计记"有照片 + 尺寸"就够了，照片本体仍可从 `data/backups/` 的库里恢复。
    """
    before = dict(row)
    try:
        content = _load(before.get("content"))
    except Exception:
        return before
    if not isinstance(content, dict):
        return before
    photo = content.get("photo")
    if isinstance(photo, dict) and photo.get("data_base64"):
        content = dict(content)
        content["photo"] = {"mime": photo.get("mime"), "width": photo.get("width"),
                            "height": photo.get("height"),
                            "data_base64": "（%d 字符，未入审计；照片见 data/backups 里的库）"
                                           % len(photo["data_base64"])}
        before["content"] = _dump(content)
    return before


def delete_resume(resume_id):
    """
    删掉一份简历（删除前的完整内容进审计）。删掉的是默认那份时，把剩下第一份提为默认。
    """
    promoted = None
    with store_module.db().locked_cursor() as cur:
        row = cur.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
        if not row:
            return "简历不存在"
        before = _audit_row(row)
        cur.execute("DELETE FROM resumes WHERE id=?", (resume_id,))
        if before.get("is_default"):
            nxt = cur.execute(
                "SELECT id FROM resumes ORDER BY sort_order ASC, id ASC LIMIT 1").fetchone()
            if nxt:
                promoted = nxt["id"]
                cur.execute("UPDATE resumes SET is_default=1, updated_at=? WHERE id=?",
                            (now_str(), promoted))
    log_change("delete", "resume", resume_id,
               "删除简历：%s" % (before.get("title") or ""), before=before, after=None)
    if promoted is not None:
        log_change("update", "resume", promoted, "默认简历顺延为 id=%s" % promoted,
                   before=None, after={"id": promoted, "is_default": 1})
    return None


def duplicate_resume(resume_id):
    """复制一份简历（决策 #2 要的"复制"）。正文与排版都深拷贝，标题加「（副本）」。"""
    stamp = now_str()
    with store_module.db().locked_cursor() as cur:
        row = cur.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
        if not row:
            return None, "简历不存在"
        source = dict(row)
        content = _read_content(source.get("content"))
        layout = _read_layout(source.get("layout"))
        base_title = clean(source.get("title"), MAX_TITLE) or "未命名简历"
        title = (base_title + "（副本）")[:MAX_TITLE]
        cur.execute(
            """
            INSERT INTO resumes (title, is_default, sort_order, content, layout,
                                 content_ver, rev, created_at, updated_at)
            VALUES (?, 0, ?, ?, ?, ?, 1, ?, ?)
            """,
            (title, as_int(source.get("sort_order")) or 500,
             _dump(deep_clone(content)), _dump(deep_clone(layout)),
             source.get("content_ver") or CONTENT_VER, stamp, stamp),
        )
        new_id = cur.lastrowid
    log_change("create", "resume", new_id, "复制简历：%s → %s" % (base_title, title),
               before=None, after={"id": new_id, "title": title, "copied_from": resume_id})
    return new_id, None


def export_all(cur=None):
    """
    数据包导出用：**完整**的简历列表（content / layout 是对象，不是库里的 JSON 文本）。

    为什么要单独一个函数：`build_state()` 里那份简历只有元信息（省流量），
    直接拿它导出会得到一堆空壳 —— 这是 2026-10-10 补数据包时踩到的点。
    """
    sql = "SELECT * FROM resumes ORDER BY is_default DESC, sort_order ASC, id ASC"
    if cur is not None:
        return [_full(row) for row in cur.execute(sql)]
    with store_module.db().locked_cursor() as own_cur:
        return [_full(row) for row in own_cur.execute(sql)]


def import_rows(cur, rows):
    """
    数据包导入用：把 `core.packaging.package_resumes()` 规整好的行写进库。

    **调用方负责事务与「要不要清空」**（见 server.py：老数据包没有 resumes 键时不许清）。
    默认那份由数据包说了算：包里有默认的，先把现有默认清掉，再只留包里第一个。
    返回写入条数。
    """
    rows = list(rows or [])
    if not rows:
        return 0
    if any(row.get("is_default") for row in rows):
        cur.execute("UPDATE resumes SET is_default=0 WHERE is_default<>0")
    count = 0
    default_done = False
    for row in rows:
        is_default = 0
        if row.get("is_default") and not default_done:
            is_default = 1
            default_done = True
        cur.execute(
            """
            INSERT INTO resumes (title, is_default, sort_order, content, layout,
                                 content_ver, rev, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (row["title"], is_default, row["sort_order"], _dump(row["content"]),
             _dump(row["layout"]), row["content_ver"], row["rev"],
             row["created_at"], row["updated_at"]),
        )
        count += 1
    return count


def set_default_resume(resume_id):
    """把某一份设为默认（其余清掉 is_default）。"""
    with store_module.db().locked_cursor() as cur:
        row = cur.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
        if not row:
            return "简历不存在"
        stamp = now_str()
        cur.execute("UPDATE resumes SET is_default=0 WHERE is_default<>0 AND id<>?",
                    (resume_id,))
        cur.execute("UPDATE resumes SET is_default=1, updated_at=? WHERE id=?",
                    (stamp, resume_id))
    log_change("update", "resume", resume_id, "设为默认简历", before=None,
               after={"id": resume_id, "is_default": 1})
    return None
