# -*- coding: utf-8 -*-
"""
services/state.py —— `build_state()`：组装前端要的完整 state

2026-09-24 从 `server.py` 搬过来（S4 的关键一步）。选它先搬的理由：
**它是所有 7 个 `api/` 模块都依赖的、唯一一个共同的名字** ——
`api/*.py` 里那句 `from server import build_state` 是「api 反过来依赖 server」
那条欠账的真正收口点。搬走它，七个模块就都能把那句删掉。

搬的时候**只改了「那三个常量/状态的来源」，业务逻辑一行没动**：
原来它直接读 `server.py` 的 `APP_VERSION` / `SCHEMA_VERSION` / `DESKTOP_MODE` /
`_STARTUP_BACKUP`，现在这些由 `server.py` 在启动时用 `configure()` 注入 ——
这样这一层**不必回头 import server**（那条依赖一旦回头，这一层就白建了）。

【规矩】
- 不 import `server`（靠 `store` / `core` / `services.notes` 和 `configure()` 注入的值）。
- 返回值形状**一个字没改**：前端、`api/` 的自检都按那个形状用。
- ⚠️ `_STARTUP_BACKUP` 是**启动期才填的**（`main()` 里跑完备份才知道），
  所以 `set_backup()` 必须在 `main()` 里调；自检脚本直接调 `init_db()` 时为 None
  —— 这是**原行为**（注释里写着「自检脚本直接调 init_db() 时为 null」），照旧。
"""

import store as store_module
from core.stages import attach_derived_fields
from core.stats import compute_stats
from core.values import now_str

from services.notes import list_notes
from services.profile import profile_status
from services.resumes import list_resumes

# 由 server.py 在启动时注入（见 configure / set_backup）。
_meta = {
    "app_version": "",
    "schema_version": 0,
    "desktop": False,
    "backup": None,
    # 这一份是「个人版」还是「正式版」（2026-09-25 加）。判断依据由 server.py 给，
    # 这一层不自己算 —— 它拿不到也不该拿文件路径。
    "edition": "",
}


def configure(app_version, schema_version, desktop, edition=""):
    """server.py 启动时调：把版本号、「是不是桌面模式」和版别注进来。"""
    _meta["app_version"] = app_version
    _meta["schema_version"] = schema_version
    _meta["desktop"] = bool(desktop)
    _meta["edition"] = edition or ""


def set_backup(info):
    """
    把本次启动做的备份结果记下来（`main()` 里跑完 `backup.run_startup_backup()` 之后调）。

    没备份（或自检直接调 init_db）时就是 None —— 和原来 `_STARTUP_BACKUP["info"]` 一样。
    """
    _meta["backup"] = info


def backup_info():
    """读回当前记录的备份结果（自检用）。"""
    return _meta["backup"]


def build_state():
    """
    返回前端需要的完整数据。

    会为每条 application 计算派生字段：
      events / event_count / current_stage / current_result / last_event_at / derived_status
    前端只读这些派生字段，不要在前端重复推导。

    ⚠️ **一次调用只用一条连接**（2026-09-24 改）。原来这里是「with LOCK 里开一条连接读四张表、
    关掉」，然后在 return 里 `list_notes()` —— 而 `list_notes(cur=None)` 会**再开第二条连接**。
    读接口和写接口都要返回完整 state，所以每次前端点一下都是「2 条连接 + 5 次全表扫」。
    现在把同一个 cursor 传给它（`list_notes` 本来就有 `cur=None` 这个参数，
    只是调用方一直没传 —— 这个参数当初就是为这件事留的）。

    锁的语义没变：整个函数仍然在锁内，`list_notes()` 被别人单独调用时也仍然走它自己的加锁路径。
    """
    with store_module.db().locked_cursor() as cur:
        apps = [dict(row) for row in cur.execute("SELECT * FROM applications ORDER BY id ASC")]
        events = [
            dict(row)
            for row in cur.execute("SELECT * FROM events ORDER BY created_at ASC, id ASC")
        ]
        stages = [
            dict(row)
            for row in cur.execute("SELECT * FROM stages ORDER BY sort_order ASC, id ASC")
        ]
        talks = [
            dict(row)
            for row in cur.execute("SELECT * FROM talks ORDER BY talk_date ASC, id ASC")
        ]
        notes = list_notes(cur)
        # 简历：与 notes 同一个约定 —— 共用 build_state() 这条连接，一次请求只开一条库连接。
        resumes = list_resumes(cur)

    # 派生字段（events / event_count / current_stage / current_result /
    # last_event_at / derived_status）的算法在 core/stages.py（2026-09-24 从原函数搬过去，
    # 那边有离线测试）。搬的时候一行没改行为：events 仍然是上面那句 SQL 的顺序
    # （created_at ASC, id ASC），就地补字段。
    attach_derived_fields(apps, events)

    return {
        "applications": apps,
        "stages": stages,
        "talks": talks,
        "stats": compute_stats(apps),
        "server_time": now_str(),
        "app_version": _meta["app_version"],
        "schema_version": _meta["schema_version"],
        # "personal" = 个人版（自己用、有个人画像/数据）/"release" = 正式版（给别人装）
        "edition": _meta["edition"],
        # 前端靠这个字段判断要不要显示「退出程序」、要不要发心跳
        "desktop": _meta["desktop"],
        "notes": notes,
        # 简历（2026-10-10 加）：**只给元信息**（id / 标题 / 是否默认 / rev / 时间），
        # 不带 content —— 正文按需 GET /api/resumes/<id> 取，免得每次点一下
        # 都把几十 KB 的简历正文传一遍。前端「简历」视图打开时再拉那一份。
        "resumes": resumes,
        # 个人画像状态（2026-09-25 加）：前端首页靠它决定要不要显示
        # 「第一次用，先花一分钟填一下」那条引导。见 core/profile.py。
        "profile": profile_status(),
        # 本次启动做的备份（见 backup.py）；自检脚本直接调 init_db() 时为 null
        "backup": _meta["backup"],
    }
