# -*- coding: utf-8 -*-
"""
api/resumes.py —— 简历：查 / 增 / 改 / 删（外加 复制、设为默认）

【约定】（照 `api/notes.py`）
- 方法签名固定：GET/POST/PUT 是 `(self, path[, payload])`，DELETE 是 `(self, path)`；
  命中并已响应返回 True，没命中返回 False。
- 调用顺序由 `server.py` 的 `GET_CHAIN / POST_CHAIN / PUT_CHAIN / DELETE_CHAIN` 决定。
- **不 import `server`**（`tools/test_architecture.py` 的 R7/R9 盯着）：业务走 `services/`，
  要 `build_state()` 也在方法里从 `services.state` 拿。
- 写接口照既有约定：处理完统一返回 `build_state()`，前端整体刷新。

【并发】保存用 `rev`：对不上时返回 **409** 并带上服务端当前 `rev`，
前端要提示用户「这份简历在别处被改过」，让用户显式选择，不许静默覆盖。
"""
import re
from urllib.parse import quote

from services import resumes as resumes_service


class ResumesRoutes:
    def get_resumes(self, path):
        """GET 简历：列表 / 单份 / 导出 PPTX。"""
        if path == "/api/resumes":
            self._send_json({"resumes": resumes_service.list_resumes()})
            return True
        match = re.match(r"^/api/resumes/(\d+)/export[.]pptx$", path)
        if match:
            item = resumes_service.get_resume(int(match.group(1)))
            if item is None:
                self._send_json({"error": "简历不存在"}, 404)
                return True
            from services.resume_pptx import PPTX_MIME, build as build_pptx

            body = build_pptx(item["content"], item["layout"])
            filename = quote((item.get("title") or "简历") + ".pptx")
            self._send_bytes(body, PPTX_MIME, 200,
                             {"Content-Disposition": "attachment; filename*=UTF-8''" + filename})
            return True
        match = re.match(r"^/api/resumes/(\d+)$", path)
        if match:
            item = resumes_service.get_resume(int(match.group(1)))
            if item is None:
                self._send_json({"error": "简历不存在"}, 404)
                return True
            self._send_json(item)
            return True
        return False

    def post_resumes(self, path, payload):
        """POST 简历：新建 / 复制 / 设为默认。"""
        from services.state import build_state

        if path == "/api/resumes":
            resume_id, error = resumes_service.create_resume(payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json({"id": resume_id, "state": build_state()})
            return True

        match = re.match(r"^/api/resumes/(\d+)/duplicate$", path)
        if match:
            new_id, error = resumes_service.duplicate_resume(int(match.group(1)))
            if error:
                self._send_json({"error": error}, 404)
                return True
            self._send_json({"id": new_id, "state": build_state()})
            return True

        match = re.match(r"^/api/resumes/(\d+)/default$", path)
        if match:
            error = resumes_service.set_default_resume(int(match.group(1)))
            if error:
                self._send_json({"error": error}, 404)
                return True
            self._send_json(build_state())
            return True
        return False

    def put_resumes(self, path, payload):
        """PUT 简历：保存（要带 rev）。"""
        from services.state import build_state

        match = re.match(r"^/api/resumes/(\d+)$", path)
        if match:
            resume_id = int(match.group(1))
            error, conflict = resumes_service.save_resume(resume_id, payload)
            if conflict is not None:
                self._send_json({
                    "error": "这份简历在别处被改过，请先确认要用哪一份",
                    "conflict": True,
                    "id": resume_id,
                    "current_rev": conflict.get("rev"),
                    "updated_at": conflict.get("updated_at"),
                }, 409)
                return True
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        return False

    def delete_resumes(self, path):
        """DELETE 简历。"""
        from services.state import build_state

        match = re.match(r"^/api/resumes/(\d+)$", path)
        if match:
            error = resumes_service.delete_resume(int(match.group(1)))
            if error:
                self._send_json({"error": error}, 404)
                return True
            self._send_json(build_state())
            return True
        return False
