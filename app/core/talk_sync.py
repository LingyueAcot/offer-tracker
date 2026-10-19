# -*- coding: utf-8 -*-
"""
core/talk_sync.py —— 宣讲会同步的**判断逻辑**（纯函数）：
哪条算重复、哪条该并进哪条、哪条该标「已取消 / 已过期」、哪些要补抓详情页。

【为什么单独一个文件】
2026-09-22 之前这些判断夹在 sync_gdut_talks() 的 SQL 之间。那条路是**全项目唯一
「联网 + 往真库写 + 自检里被明确跳过」**的：tools/test_routes.py 里写着
    BLOCKED = {("POST", "/api/sync_talks")}      # 会真的去抓学校网站，不自检
解析那一半（parse_talk_list / parse_talk_detail / company_key…）早就搬进 core 测过了，
可「决定数据怎么变」的规则一条都没有测试 —— 只能靠真跑一次同步来验，
而那正是最不该拿真库做的实验。所以把判断搬到这儿，用离线 fixture 测
（tools/test_core.py）。

【规矩】同 core/talks.py：不许 import server / sqlite3 / urllib / subprocess / socket /
threading，不许 open() 文件。搬的时候是逐字照抄 + 只把「顺手改调用方列表」这类
副作用摘出去（见 match_loose 的说明）。
"""

from core.talks import company_key


def merge_key(item):
    """
    分页之间判重用：优先 gdut_id；没有就用「公司 + 日期」。

    为什么要有兜底：gdut_id 是学校网站的 id，个别条目可能没解析出来
    （parse_talk_list 里解析不到就给空串）。那种情况下至少别让同一家公司
    同一天的宣讲会在库里出现两遍。
    """
    return item.get("gdut_id") or ((item.get("company") or "") + (item.get("talk_date") or ""))


def dedupe_items(items):
    """
    同一场宣讲会在分页里出现多次时只留**第一条**（顺序保持不变）。

    列表页是分页抓下来的，翻页之间会重叠；不先去重，后面「新增」就会把同一场插两遍。
    """
    seen = set()
    out = []
    for item in items:
        key = merge_key(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def match_loose(candidates, item):
    """
    在「还没绑定 gdut_id」的记录里找一个同公司的，用于**合并**而不是新建。

    两步：先按归一化公司名**完全相同**找；找不到再退一步做**子串**匹配
    （两边都要求长度 >= 3，免得「中」这种短名字乱配）。找不到返回 None。

    **不改 candidates**：老代码是在这里顺手把命中的那条从候选里删掉的，
    搬进纯函数之后由调用方删（命中后紧跟一句 candidates.remove(matched)），
    这样这个函数才真的「给同样的输入就给同样的输出」，也才对得起 core/ 的规矩。
    """
    key = company_key(item.get("company_std") or item.get("company") or "")
    if not key:
        return None
    for candidate in candidates:
        cand_key = company_key(candidate.get("company_std") or candidate.get("company") or "")
        if cand_key and cand_key == key:
            return candidate
    for candidate in candidates:
        cand_key = company_key(candidate.get("company_std") or candidate.get("company") or "")
        if len(cand_key) >= 3 and (cand_key in key or key in cand_key):
            return candidate
    return None


def items_needing_detail(items, existing_by_gdut_id):
    """
    哪些要补抓详情页：库里这条**还没有 detail_text** 的。

    已经抓过的不再抓 —— 详情页几十个，全抓一遍是几倍的等待时间，而详情内容基本不变。
    """
    out = []
    for item in items:
        old = existing_by_gdut_id.get(item.get("gdut_id") or "") or {}
        if not (old.get("detail_text") or ""):
            out.append(item)
    return out


def plan_cleanup(rows, imported_ids, start_date, end_date):
    """
    同步完之后要维护的状态，返回 [(id, 新状态), ...]（同一个 id 最多出现一次）。

    两条规则，都**不碰你自己填的东西**：

      1. **页面上查不到了**：库里的记录如果「是同步来的、还挂着待参加、在本次窗口内」，
         却不在这次抓回来的 imported_ids 里，说明学校页面撤了它 ——
         日期已经过去 -> 已过期；还没到 -> 已取消。
         （注意：窗口内的日期不可能早于 start_date，所以这条实际恒为「已取消」。
           保留这个判断是为了和原逻辑逐字一致，别当成 bug 顺手改掉。）
      2. **日期已经过去**、还挂着「待参加」的（不在本次窗口里）-> 已过期，
         这样它们会自动收进宣讲会页面底部的折叠区，不占主列表。

    只处理 source == 'gdut' 且 status == '待参加' 的记录：你手填的、
    或者你已经改过状态（已参加 / 已投递 …）的，一律不碰。

    imported_ids 为空时**不做第 1 条** —— 原逻辑就是「一条都没解析出 gdut_id 就不做消失判定」，
    否则一次解析失败会把整页宣讲会全标成已取消。
    """
    imported = set(imported_ids or [])
    out = []
    for row in rows:
        if (row.get("source") or "") != "gdut":
            continue
        if (row.get("status") or "") != "待参加":
            continue
        date = row.get("talk_date") or ""
        if not date:
            continue
        gdut_id = row.get("gdut_id") or ""
        if imported and gdut_id and gdut_id not in imported and start_date <= date <= end_date:
            out.append((row["id"], "已过期" if date < start_date else "已取消"))
        elif date < start_date:
            out.append((row["id"], "已过期"))
    return out
