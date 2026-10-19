# -*- coding: utf-8 -*-
"""
api/notes.py —— 备忘录：增 / 改 / 删

【这个文件是什么】
从 server.py 的 Handler 里按域拆出来的路由（2026-09-22）。每个方法对应一个 HTTP 方法，
方法体里的分支**逐字搬自原来的 do_GET / do_POST / do_PUT / do_DELETE**，
唯一改动是把「分支体里的裸 return」写成了 return True（表示这条请求我处理完了）。

【约定】
- 方法签名固定 (self, path)，命中并已响应返回 True，没命中返回 False；
- 调用顺序由 server.py 的 do_* 决定；域内分支顺序 = 原来文件里的顺序，不许乱；
- **业务动作从这里改走 `services/`**（2026-09-24，方案 S4）：备忘录那几个函数搬进了
  `services/notes.py`，所以本文件 import 的是 `services.notes`，**不再 import `server`** ——
  那条「api/ 反过来依赖 server」的欠账就是这么一条条还掉的。
- 变量名用 `notes_service`（不是 `from services.notes import create_note`）：这样
  自检脚本想替换实现时，替换一处就够，不用动每个调用点。
- ⚠️ **`build_state` 目前还得从 `server` 拿**（它还没搬进 services）。它是最后一条欠账，
  等 `build_state` 也搬走，本文件里那句 `from server import build_state` 就删掉。
"""
import re

from services import notes as notes_service


class NotesRoutes:
    def post_notes(self, path, payload):
        """POST 备忘录：增 的路由（逐字搬自 do_POST）。"""
        from services.state import build_state
        if path == "/api/notes":
            _, error = notes_service.create_note(payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        return False

    def put_notes(self, path, payload):
        """PUT 备忘录：改 的路由（逐字搬自 do_PUT）。"""
        from services.state import build_state
        match = re.match(r"^/api/notes/(\d+)$", path)
        if match:
            error = notes_service.update_note(int(match.group(1)), payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        return False

    def delete_notes(self, path):
        """DELETE 备忘录：删 的路由（逐字搬自 do_DELETE）。"""
        from services.state import build_state
        match = re.match(r"^/api/notes/(\d+)$", path)
        if match:
            notes_service.delete_note(int(match.group(1)))
            self._send_json(build_state())
            return True
        return False
