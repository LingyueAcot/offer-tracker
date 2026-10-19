# -*- coding: utf-8 -*-
"""
api/core.py —— 核心读接口：state / ping / changelog / schema / profile / diagnostics

【这个文件是什么】
从 server.py 的 Handler 里按域拆出来的路由（2026-09-22）。每个方法对应一个 HTTP 方法，
方法体里的分支**逐字搬自原来的 do_GET / do_POST / do_PUT / do_DELETE**，
唯一改动是把「分支体里的裸 return」写成了 return True（表示这条请求我处理完了）。

【约定】
- 方法签名固定 (self, path)，命中并已响应返回 True，没命中返回 False；
- 调用顺序由 server.py 的 do_* 决定；域内分支顺序 = 原来文件里的顺序，不许乱；
- 这里用到的 server 里的函数/常量，靠文件底部那句 from server import ... 拿。
"""


class CoreRoutes:
    def get_core(self, path):
        """GET 核心读接口：state / ping / 的路由（逐字搬自 do_GET）。"""
        from server import CHANGELOG_PATH, load_profile, now_str, read_changelog, schema_description
        from services.state import build_state
        if path == "/api/state":
            self._send_json(build_state())
            return True
        if path == "/api/ping":
            self._send_json({"ok": True, "time": now_str()})
            return True
        if path == "/api/changelog":
            entries = read_changelog()
            self._send_json({"entries": entries, "total": len(entries), "file": CHANGELOG_PATH})
            return True
        if path == "/api/schema":
            self._send_json(schema_description())
            return True
        if path == "/api/profile":
            self._send_json(load_profile())
            return True
        if path == "/api/diagnostics":
            # 诊断报告（2026-09-25 加）：把**这台机器**的运行环境 + 出错现场打成一个
            # 纯文本文件下载。给用户一个"把问题发给开发者"的途径。
            # ⚠️ 报告里绝不包含 data/applications.db、change_log.jsonl、个人画像.json 的内容
            #    —— 这一条由 tools/test_diagnostics.py 断言（拿真库抽样本反过来查报告）。
            import server as _srv
            from services import diagnostics as diag
            from services.state import build_state
            state = build_state()
            counts = {
                "投递": len(state.get("applications") or []),
                "宣讲会": len(state.get("talks") or []),
                "阶段": len(state.get("stages") or []),
                "备忘": len(state.get("notes") or []),
            }
            backup = state.get("backup") or {}
            backup_text = "成功" if backup.get("dir") else (
                "未做/失败" if state.get("desktop") is not None else "")
            env = diag.collect_env(_srv.APP_VERSION, state.get("edition") or "",
                                   state.get("desktop"), counts, backup_text)
            report = diag.build_diagnostic_report(
                env, _srv.APP_VERSION, state.get("edition") or "")
            # 文件名用纯 ASCII 时间戳：中文文件名走 Content-Disposition 要额外编码，
            # 而这个名字只是给用户认"哪一份"，不需要好看。
            import time as _time
            name = "diagnostics_%s.txt" % _time.strftime("%Y%m%d_%H%M%S")
            self._send_bytes(
                report.encode("utf-8"),
                "text/plain; charset=utf-8",
                200,
                {"Content-Disposition": "attachment; filename*=UTF-8''" + _srv._quote(name)},
            )
            return True
        if path == "/api/open_logs":
            # 在资源管理器里打开 logs 目录（2026-09-25 加）。
            # 无参数、只开这一个目录 —— 不做成"打开任意路径"。
            from server import open_logs_folder
            ok = open_logs_folder()
            self._send_json({"ok": bool(ok)}, 200 if ok else 500)
            return True
        return False
