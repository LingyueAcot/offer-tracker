# -*- coding: utf-8 -*-
"""
api/apps.py —— 投递与进展：applications / events / stages / restore / split

【这个文件是什么】
从 server.py 的 Handler 里按域拆出来的路由（2026-09-22）。每个方法对应一个 HTTP 方法，
方法体里的分支**逐字搬自原来的 do_GET / do_POST / do_PUT / do_DELETE**，
唯一改动是把「分支体里的裸 return」写成了 return True（表示这条请求我处理完了）。

【约定】
- 方法签名固定 (self, path)，命中并已响应返回 True，没命中返回 False；
- 调用顺序由 server.py 的 do_* 决定；域内分支顺序 = 原来文件里的顺序，不许乱；
- **业务动作从这里改走 `services/`**（2026-09-24，S4）：投递/进展/阶段那 10 个函数
  搬进了 `services/apps.py`，本文件 import 它，**不再 import `server`**。
- 自检要替换实现时，换 `apps_service` 上那一个名字就够，不用动每个调用点。
- 2026-09-24 顺手删掉了 `delete_apps` 里那句 `path = urlparse(self.path).path` ——
  `server.py` 的 `do_DELETE` **已经解析好 path 才传进来**，重解析一遍既多余又容易误导
  （另外两个方法都不这么写）。
"""
import re

from services import apps as apps_service
from services.state import build_state


class AppsRoutes:
    def post_apps(self, path, payload):
        """POST 投递与进展：applications / events / stages / restore / split 的路由。"""
        match = re.match(r"^/api/applications/(\d+)/split$", path)
        if match:
            _, error = apps_service.split_application(int(match.group(1)))
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        if path == "/api/applications":
            _, error = apps_service.create_application(payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        if path == "/api/events":
            _, error = apps_service.create_event(payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        if path == "/api/stages":
            error = apps_service.add_stage(payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        if path == "/api/restore":
            _, error = apps_service.restore_application(payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        return False

    def put_apps(self, path, payload):
        """PUT 投递与进展：applications / events 的路由。"""
        match = re.match(r"^/api/applications/(\d+)$", path)
        if match:
            error = apps_service.update_application(int(match.group(1)), payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        match = re.match(r"^/api/events/(\d+)$", path)
        if match:
            error = apps_service.update_event(int(match.group(1)), payload)
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        return False

    def delete_apps(self, path):
        """DELETE 投递与进展：applications / events / stages 的路由。"""
        match = re.match(r"^/api/applications/(\d+)$", path)
        if match:
            apps_service.delete_application(int(match.group(1)))
            self._send_json(build_state())
            return True
        match = re.match(r"^/api/events/(\d+)$", path)
        if match:
            apps_service.delete_event(int(match.group(1)))
            self._send_json(build_state())
            return True
        match = re.match(r"^/api/stages/(\d+)$", path)
        if match:
            error = apps_service.delete_stage(int(match.group(1)))
            if error:
                self._send_json({"error": error}, 400)
                return True
            self._send_json(build_state())
            return True
        return False
