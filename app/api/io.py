# -*- coding: utf-8 -*-
"""
api/io.py —— 导入导出：Excel 模板、JSON / CSV 导出、JSON / Excel 导入

【这个文件是什么】
从 server.py 的 Handler 里按域拆出来的路由（2026-09-22）。每个方法对应一个 HTTP 方法，
方法体里的分支**逐字搬自原来的 do_GET / do_POST / do_PUT / do_DELETE**，
唯一改动是把「分支体里的裸 return」写成了 return True（表示这条请求我处理完了）。

【约定】
- 方法签名固定 (self, path)，命中并已响应返回 True，没命中返回 False；
- 调用顺序由 server.py 的 do_* 决定；域内分支顺序 = 原来文件里的顺序，不许乱；
- 这里用到的 server 里的函数/常量，靠文件底部那句 from server import ... 拿。
"""
import os

class IoRoutes:
    def get_io(self, path):
        """GET 导入导出：Excel 模板、JSON / 的路由（逐字搬自 do_GET）。"""
        from server import AI_PROMPT_PATH, EXCEL_TEMPLATE_PATH, _quote, import_excel_package, import_package
        from services.state import build_state
        if path == "/api/ai_prompt":
            # 给页面上「复制给 AI 的转换提示词」按钮用：把那份 md 原样吐出来
            if not os.path.isfile(AI_PROMPT_PATH):
                self._send_json({"error": "提示词文件不存在"}, 404)
                return True
            with open(AI_PROMPT_PATH, "r", encoding="utf-8") as handle:
                body = handle.read().encode("utf-8")
            self._send_bytes(body, "text/markdown; charset=utf-8", 200)
            return True
        if path == "/api/template.xlsx":
            if not os.path.isfile(EXCEL_TEMPLATE_PATH):
                self._send_json({"error": "模板文件不存在"}, 404)
                return True
            with open(EXCEL_TEMPLATE_PATH, "rb") as handle:
                body = handle.read()
            self._send_bytes(
                body,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                200,
                {"Content-Disposition": "attachment; filename*=UTF-8''" + _quote("导入模板.xlsx")},
            )
            return True
        if path == "/api/export.json":
            self._export_json()
            return True
        if path == "/api/export.csv":
            self._export_csv()
            return True
        return False

    def post_io(self, path, payload):
        """POST 导入导出：Excel 模板、JSON / 的路由（逐字搬自 do_POST）。"""
        from server import EXCEL_TEMPLATE_PATH, _quote, import_excel_package, import_package
        from services.state import build_state
        if path == "/api/import":
            error = import_package(payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        if path == "/api/import_excel":
            error = import_excel_package(payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        return False
