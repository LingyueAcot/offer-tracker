# -*- coding: utf-8 -*-
"""
services/ —— 编排层：每个域一个模块，放「读 → 算 → 写 → 记审计」这一整套用例

【这一层是干什么的】
夹在 `api/`（认路径、发响应）和 `store.py` / `core/`（搬数据、纯计算）中间。

    server.py  →  api/  ──────▶  services/  ──────▶  store.py
                                                     core/
    （箭头只有一个方向：上面可以叫下面，下面**不许回头叫上面**）

【为什么要它】（2026-09-24 新增，见 `docs/架构.md` 的 S4/S5）
原来没有这一层，业务动作只能写在 `server.py` 里 —— 于是 `api/` 七个模块
**全都反过来 import server** 才拿得到东西。审计出来的结论是：不是「搬得不够」，
是**中间少了一层**。

有了 `services/`：
- `api/` 只认 `services/`，不再认 `server`（那条反向依赖就断了）；
- `server.py` 退回「起服务 + 装路由 + HTTP 管道」，不再堆业务；
- 加一个新域 = 新增 `core/xxx.py` + `services/xxx.py` + `api/xxx.py` 三个文件，
  再往 `server.py` 的两条链里各加一行。

【规矩】（改这里之前先读）
- **不许 import `server`**。需要什么就从 `store` / `core` / 本包内拿 ——
  一旦回头 import `server`，这一层就白建了。这条由 `tools/test_architecture.py` 盯着。
- 不许自己 `sqlite3.connect`，一律走 `store.db().cursor()` / `locked_cursor()`。
- 不含 HTTP 细节（不碰 self、不拼响应），那是 `api/` 的事。
- 每个写操作都要记审计（`services.audit.log_change`），否则改动日志断链。
- 纯规则放 `core/`，这里只做编排。

【这一层现在有什么】
- `audit.py`  改动日志读写（先拆它，是为了让各域不必回头找 server）
- `notes.py`  备忘录
（其余各域还在 `server.py` 里，按 `docs/架构.md` 的 S4 一个个搬过来）
"""
