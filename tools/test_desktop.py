# 桌面版自检：备忘录接口 + 心跳 + 退出接口
# 用法：python work\test_desktop.py [端口]     端口默认 8766（2.0）；v3 用 8768
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

PORT = sys.argv[1] if len(sys.argv) > 1 else "8766"
BASE = "http://127.0.0.1:%s" % PORT
ok = True
print("检查目标：" + BASE)


def call(method, path, payload=None, timeout=15):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def check(label, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("[PASS] " if cond else "[FAIL] ") + label + ("  " + extra if extra else ""))


def skip(label, extra=""):
    """数据不满足前提时用它：打 [SKIP]、**不影响退出码**（照 AGENTS.md 第二节的约定）。"""
    print("[SKIP] " + label + ("  " + extra if extra else ""))


status, state = call("GET", "/api/state")
check("桌面版接口可用", status == 200, "投递=%d" % state["stats"]["total"])
check("state 里带 desktop 标记", state.get("desktop") is True)
check("state 里带 notes 数组", isinstance(state.get("notes"), list))

# 启动备份（app/backup.py）：接口里报了，还得真的落在磁盘上、而且能打开才算数
backup = state.get("backup")
# ⚠️ **首次运行**（还没有库）时 `backup` 按接口契约就是 `null` —— 没东西可备，不是失败。
#    原来这里直接判 FAIL，于是在干净检出 / CI 的空库上必红（2026-10-11 跑 2B 基线时就是这么红的）。
#    判据用接口能拿到的信息：**库里 0 条**才按 [SKIP]；已经有数据却拿不到备份 → 仍然报 FAIL（不能掩护真故障）。
first_run = (backup is None) and int(state.get("stats", {}).get("total", 0)) == 0
if first_run:
    skip("state 里带本次启动的备份信息（首次运行、库里 0 条，按契约是 null）")
else:
    check("state 里带本次启动的备份信息", isinstance(backup, dict) and backup.get("ok") is True,
          "" if isinstance(backup, dict) else "（有数据却没有备份信息）")
if isinstance(backup, dict):
    db_copy = os.path.join(backup.get("dir") or "", "applications.db")
    check("备份的副本真的在磁盘上", os.path.isfile(db_copy), backup.get("dir", ""))
    if os.path.isfile(db_copy):
        conn = sqlite3.connect(db_copy)
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        conn.close()
        check("备份的副本能打开且完整", quick == "ok", "quick_check=%s" % quick)

before = len(state.get("notes") or [])
status, state = call("POST", "/api/notes", {"text": "__自检用的备忘录__"})
notes = state.get("notes") or []
check("新增备忘录", status == 200 and len(notes) == before + 1, "现在 %d 条" % len(notes))
note = [n for n in notes if n["text"] == "__自检用的备忘录__"]
check("新备忘录默认未完成", bool(note) and note[0]["done"] == 0)

if note:
    note_id = note[0]["id"]
    status, state = call("PUT", "/api/notes/%d" % note_id, {"done": True})
    hit = [n for n in state["notes"] if n["id"] == note_id][0]
    check("打勾后 done 变 1", hit["done"] == 1)
    status, state = call("PUT", "/api/notes/%d" % note_id, {"text": "__改过一句话__"})
    hit = [n for n in state["notes"] if n["id"] == note_id][0]
    check("能改备忘录内容", hit["text"] == "__改过一句话__", hit["text"])
    status, state = call("DELETE", "/api/notes/%d" % note_id)
    left = [n for n in state["notes"] if n["id"] == note_id]
    check("能删备忘录", status == 200 and not left, "剩 %d 条" % len(state["notes"]))

# ---------------------------------------------------------------- 投递与进展
# 这几条在 开发记录 第 9 节的清单里挂了很久，但从没有自检盯着（只有路由被扫过"不是 404"）。
# **自造自删**：只动自己建的「__自检用投递__」，跑完删干净；真数据一条不碰。
before_total = state["stats"]["total"]
status, state = call("POST", "/api/applications",
                     {"company": "__自检用投递__", "position": "自检岗位", "stage": "投递"})
mine = [a for a in (state.get("applications") or []) if a["company"] == "__自检用投递__"]
check("新增投递", status == 200 and len(mine) == 1
      and len(state["applications"]) == before_total + 1,
      "总数 %d → %d" % (before_total, len(state["applications"])))

app = mine[0] if mine else None
first = ((app or {}).get("events") or [{}])[0]
check("新增投递默认生成「投递 · 已完成」",
      first.get("stage") == "投递" and first.get("result") == "已完成" and bool(first.get("completed_at")),
      "stage=%s result=%s" % (first.get("stage"), first.get("result")))
check("刚建好时整体状态是「进行中」", (app or {}).get("derived_status") == "进行中",
      str((app or {}).get("derived_status")))


def _app(state_, app_id):
    return [a for a in (state_.get("applications") or []) if a["id"] == app_id]


if app:
    status, state = call("POST", "/api/events",
                         {"application_id": app["id"], "stage": "一面", "result": "进行中"})
    now = _app(state, app["id"])[0]
    prev = [e for e in now["events"] if e["stage"] == "投递"]
    cur = [e for e in now["events"] if e["stage"] == "一面"]
    check("登记新阶段后，上一阶段自动变「已完成」并写上完成时间",
          bool(prev) and prev[0]["result"] == "已完成" and bool(prev[0]["completed_at"]),
          "投递 → %s / %s" % (prev[0]["result"] if prev else "?", prev[0]["completed_at"] if prev else "?"))
    check("刚落的新阶段自己是「进行中」",
          bool(cur) and cur[0]["result"] == "进行中" and not cur[0]["completed_at"])

    # 给这条进展带上备注和事件日期，后面要验证「只传 result 的 PUT 不许把它们冲掉」
    status, state = call("PUT", "/api/events/%d" % cur[0]["id"],
                         {"stage": "一面", "result": "进行中",
                          "note": "__自检备注__", "event_date": "2026-09-01"})
    hit = [e for e in _app(state, app["id"])[0]["events"] if e["id"] == cur[0]["id"]][0]
    check("编辑进展：备注 / 事件日期能写进去",
          hit["note"] == "__自检备注__" and hit["event_date"] == "2026-09-01")

    # 点绿勾 = 把当前阶段改成已完成（页面 doneBtn 发的就是这条）
    status, state = call("PUT", "/api/events/%d" % cur[0]["id"], {"result": "已完成"})
    hit = [e for e in _app(state, app["id"])[0]["events"] if e["id"] == cur[0]["id"]][0]
    check("点绿勾后当前阶段变「已完成」并写上完成时间",
          hit["result"] == "已完成" and bool(hit["completed_at"]),
          "%s / %s" % (hit["result"], hit["completed_at"]))
    # ⚠️ 这条是 2026-09-22 真正抓到 bug 的那一条：旧代码是「整条覆盖」，
    # 只传 result 会把阶段名写成默认值「进展」、把备注和事件日期清空。
    check("只传 result 的 PUT 不会把阶段名 / 备注 / 事件日期冲掉",
          hit["stage"] == "一面" and hit["note"] == "__自检备注__" and hit["event_date"] == "2026-09-01",
          "stage=%r note=%r date=%r" % (hit["stage"], hit["note"], hit["event_date"]))
    status, state = call("PUT", "/api/events/%d" % cur[0]["id"], {"stage": ""})
    check("显式传空阶段名会被拒绝（不写默认值）",
          status == 400, "HTTP %d" % status)

    # 删除 → 改动日志留了删除前的完整数据 → 用那份数据撤销删除
    status, state = call("DELETE", "/api/applications/%d" % app["id"])
    check("删除投递", status == 200 and not _app(state, app["id"]),
          "总数 %d" % len(state["applications"]))
    status, log = call("GET", "/api/changelog")
    deletes = [e for e in (log.get("entries") or [])
               if e.get("action") == "delete"
               and ((e.get("before") or {}).get("application") or {}).get("company") == "__自检用投递__"]
    check("改动日志留下了删除前的完整数据（含每条进展）",
          bool(deletes) and len((deletes[0]["before"] or {}).get("events") or []) == 2,
          "删掉时带走了 %d 条进展"
          % (len((deletes[0]["before"] or {}).get("events") or []) if deletes else 0))
    if deletes:
        payload = deletes[0]["before"]
        status, state = call("POST", "/api/restore",
                             {"application": payload.get("application"), "events": payload.get("events")})
        back = [a for a in (state.get("applications") or []) if a["company"] == "__自检用投递__"]
        check("撤销删除：投递和它的 2 条进展都回来了",
              status == 200 and len(back) == 1 and len(back[0]["events"]) == 2,
              "进展 %d 条" % (len(back[0]["events"]) if back else 0))
        if back:
            stages = sorted(e["stage"] for e in back[0]["events"])
            check("撤销回来的内容没走样（公司 / 岗位 / 阶段都对）",
                  back[0]["position"] == "自检岗位" and stages == ["一面", "投递"], "、".join(stages))

    # 收尾：把自检造出来的东西删干净（上一步可能已经删过一条）
    left = [a for a in (state.get("applications") or []) if a["company"] == "__自检用投递__"]
    for item in left:
        status, state = call("DELETE", "/api/applications/%d" % item["id"])
    check("自检数据清理干净（总数回到原样）",
          len(state["applications"]) == before_total,
          "%d 条（原来 %d 条）" % (len(state["applications"]), before_total))

# ---------------------------------------------------------------- 宣讲会
# 第 9 节清单里「宣讲会：新增 / 编辑 / 删除、状态一键推进」一条也从没验过。同样**自造自删**。
before_talks = len(state.get("talks") or [])
status, state = call("POST", "/api/talks",
                     {"company": "__自检用宣讲会__", "talk_date": "2026-12-01",
                      "time_slot": "14:00", "location": "自检地点", "status": "待参加"})
talks = [t for t in (state.get("talks") or []) if t["company"] == "__自检用宣讲会__"]
check("新增宣讲会", status == 200 and len(talks) == 1, "总数 %d → %d" % (before_talks, len(state.get("talks") or [])))
talk = talks[0] if talks else None
if talk:
    status, state = call("PUT", "/api/talks/%d" % talk["id"], {"company": "__自检用宣讲会__", "location": "改过的地点"})
    hit = [t for t in state["talks"] if t["id"] == talk["id"]][0]
    check("编辑宣讲会（只传要改的字段，别的不能被清空）",
          hit["location"] == "改过的地点" and hit["company"] == "__自检用宣讲会__"
          and hit.get("talk_date") == "2026-12-01",
          "地点=%r 日期=%r" % (hit["location"], hit.get("talk_date")))
    # 状态一键推进：待参加 → 已参加 → 已投递 → 已面试（页面上的按钮就是发这个）
    chain = []
    for want in ("已参加", "已投递", "已面试"):
        status, state = call("PUT", "/api/talks/%d" % talk["id"], {"status": want})
        got = [t for t in state["talks"] if t["id"] == talk["id"]][0]["status"]
        chain.append(got)
    check("宣讲会状态能一键推进（待参加 → 已参加 → 已投递 → 已面试）",
          chain == ["已参加", "已投递", "已面试"], "、".join(chain))
    status, state = call("DELETE", "/api/talks/%d" % talk["id"])
    check("删除宣讲会", status == 200 and not [t for t in state["talks"] if t["id"] == talk["id"]])
check("自检造的宣讲会清理干净（总数回到原样）",
      len(state.get("talks") or []) == before_talks,
      "%d 场（原来 %d 场）" % (len(state.get("talks") or []), before_talks))

# ⚠️ 上面把宣讲会状态推到「已投递」时，程序会**自动建一条投递记录**（正常功能）。
# 2026-09-23 就是漏了这一步，往真库里留了一条 __自检用宣讲会__ —— 这里补上。
leftover_apps = [a for a in (state.get("applications") or [])
                 if (a.get("company") or "").startswith("__自检")]
for item in leftover_apps:
    status, state = call("DELETE", "/api/applications/%d" % item["id"])
check("宣讲会自动生成的那条投递记录也删掉了（自检不许留东西在真库里）",
      not [a for a in (state.get("applications") or [])
           if (a.get("company") or "").startswith("__自检")],
      "删了 %d 条" % len(leftover_apps))

# 前端实际用的是 POST（关窗口时 sendBeacon 也只能发 POST），所以两种都要能用
status, body = call("POST", "/api/heartbeat", {})
check("心跳接口可用（POST）", status == 200 and body.get("ok") is True)
status, body = call("POST", "/api/closing", {})
check("关闭信号接口可用（POST）", status == 200 and body.get("ok") is True)
status, body = call("GET", "/api/heartbeat")
check("心跳接口也支持 GET", status == 200 and body.get("ok") is True)

print("\n结果：" + ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
