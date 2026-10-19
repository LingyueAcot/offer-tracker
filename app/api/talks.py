# -*- coding: utf-8 -*-
"""
api/talks.py —— 宣讲会：列表、同步、调研任务包与导入

【这个文件是什么】
从 server.py 的 Handler 里按域拆出来的路由（2026-09-22）。每个方法对应一个 HTTP 方法，
方法体里的分支**逐字搬自原来的 do_GET / do_POST / do_PUT / do_DELETE**，
唯一改动是把「分支体里的裸 return」写成了 return True（表示这条请求我处理完了）。

【约定】
- 方法签名固定 (self, path)，命中并已响应返回 True，没命中返回 False；
- 调用顺序由 server.py 的 do_* 决定；域内分支顺序 = 原来文件里的顺序，不许乱；
- **宣讲会的增删改走 `services/talks.py`、调研走 `services/research.py`**（2026-09-24，S4）；
- ⚠️ **只剩「同步」一块没搬**，所以下面还有一句 `from server import ...`：
  `SYNC_STATE` 与 `start_sync_async` —— 它们绑着 `sync_gdut_talks`（237 行，
  全项目唯一联网 + 写真库的路径），等它搬进 `同步模块` 才能一起断。
  搬完这句 `from server import` 就删掉（架构检查器的 WATCH 会盯着）。
"""
import re

from services import research as research_service
from services import talks as talks_service
from services.state import build_state


class TalksRoutes:
    # 只剩「同步」这一块还没搬 —— 搬完就把这个方法和上面那句 import 一起删掉
    @staticmethod
    def _sync_pending():
        from server import SYNC_STATE, start_sync_async
        return SYNC_STATE, start_sync_async

    def get_talks(self, path):
        """GET 宣讲会：同步状态 / 调研任务包 的路由。"""
        if path == "/api/sync_status":
            SYNC_STATE, _start_sync_async = self._sync_pending()
            self._send_json({
                "running": SYNC_STATE["running"],
                "stage": SYNC_STATE["stage"],
                "message": SYNC_STATE["message"],
                "report": SYNC_STATE["report"],
                "last_sync": SYNC_STATE["last_sync"],
            })
            return True
        if path == "/api/research_task":
            # 返回 (文本, 错误信息)：画像没填主专业时给 400 + 人话提示，
            # 前端 api() 会把它弹成 toast（见 index.html 的 api()）。
            text, error = research_service.build_research_task()
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json({"text": text})
            return True
        return False

    def put_talks(self, path, payload):
        """PUT：个人画像保存 + 宣讲会修改。

        画像放在这个域是因为「调研任务包」和画像是一对 —— 画像决定调研提示词怎么写。

        ⚠️ **这个方法只能有一个定义**（2026-09-27 修）：原来这里写了一份「保存画像」的
        `put_talks`，下面「改宣讲会」又写了一份同名的，**后者把前者整个覆盖掉**，
        于是 `PUT /api/profile` 从路由表里消失、界面保存画像永远回 404
        （`tools/test_routes.py` 抽路由时只看得见活下来的那个定义，所以它照样全绿）。
        现在合并成一个：两个分支互斥，`tools/test_architecture.py` 的 R10 盯着别再写两份。
        """
        if path == "/api/profile":
            from services import profile as profile_service
            saved, error = profile_service.save_profile(payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        match = re.match(r"^/api/talks/(\d+)$", path)
        if match:
            error = talks_service.update_talk(int(match.group(1)), payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        return False

    def post_talks(self, path, payload):
        """POST 宣讲会：新增 / 触发同步 / 导入调研结果 的路由。"""
        if path == "/api/talks":
            _, error = talks_service.create_talk(payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        if path == "/api/sync_talks":
            SYNC_STATE, start_sync_async = self._sync_pending()
            started = start_sync_async(fetch_details=bool(payload.get("fetch_details", True)))
            self._send_json({"started": started, "running": SYNC_STATE["running"]})
            return True
        if path == "/api/import_research":
            result = research_service.apply_research(payload.get("text") or "")
            if not result.get("ok"):
                self._send_json({"error": result.get("error")}, 400)
                return True
            self._send_json(dict(build_state(), research_report=result))
            return True
        return False

    def delete_talks(self, path):
        """DELETE 宣讲会：删 的路由。"""
        match = re.match(r"^/api/talks/(\d+)$", path)
        if match:
            talks_service.delete_talk(int(match.group(1)))
            self._send_json(build_state())
            return True
        return False
