# -*- coding: utf-8 -*-
"""
api/desktop.py —— 桌面模式专用：心跳 / 关闭信号 / 退出（GET 和 POST 都收）

【这个文件是什么】
从 server.py 的 Handler 里按域拆出来的路由（2026-09-22）。每个方法对应一个 HTTP 方法，
方法体里的分支**逐字搬自原来的 do_GET / do_POST / do_PUT / do_DELETE**，
唯一改动是把「分支体里的裸 return」写成了 return True（表示这条请求我处理完了）。

【约定】
- 方法签名固定 (self, path)，命中并已响应返回 True，没命中返回 False；
- 调用顺序由 server.py 的 do_* 决定；域内分支顺序 = 原来文件里的顺序，不许乱；
- 这里用到的 server 里的函数/常量，靠文件底部那句 from server import ... 拿。
"""
import threading
# urlparse / parse_qs 本来就是标准库的东西 —— 以前是从 server 里转发出来的，
# 2026-09-24 改成直接从标准库 import（S4）：少一条对 server 的依赖。
from urllib.parse import parse_qs, urlparse

# 桌面生命周期的三个动作从 services/ 拿（不再 import server）。
from services.lifecycle import (attach_window, ALIVE, STATE_LOCK, request_shutdown, touch_closing,
                                touch_heartbeat)


class DesktopRoutes:
    def _handle_desktop_route(self, path):
        """
        桌面模式的三个小接口，GET 和 POST 都收：
          /api/heartbeat  页面还开着（每 20 秒一次）
          /api/closing    页面正在关闭 / 刷新（beforeunload 时用 sendBeacon 发）
          /api/shutdown   用户点了「退出程序」
        返回 True 表示这个请求已经处理完了。
        """
        if path == "/api/heartbeat":
            query = parse_qs(urlparse(self.path).query)
            try:
                source_port = int((query.get("shport") or ["0"])[0])
            except (TypeError, ValueError):
                source_port = 0
            accepted = touch_heartbeat(source_port)
            self._send_json({"ok": True, "accepted": accepted})
            return True
        if path == "/api/closing":
            # 页面用 sendBeacon 发，所以端口走查询串：/api/closing?shport=NNNN
            query = parse_qs(urlparse(self.path).query)
            try:
                source_port = int((query.get("shport") or ["0"])[0])
            except (TypeError, ValueError):
                source_port = 0
            accepted = touch_closing(source_port)
            self._send_json({"ok": True, "accepted": accepted})
            return True
        if path == "/api/shutdown":
            with STATE_LOCK:
                ALIVE["done"] = True
            self._send_json({"ok": True})
            # 先把响应发回去再退出，免得前端拿到「连接被重置」
            threading.Timer(0.4, request_shutdown).start()
            return True
        return False

    def get_desktop(self, path):
        """GET 桌面模式专用：心跳 / 关闭信号 / 退 的路由（逐字搬自 do_GET）。"""
        if self._handle_desktop_route(path):
            return True
        return False

    def post_desktop(self, path, payload):
        if path == "/api/attach":
            try:
                source_port = int(payload.get("shport") or 0)
            except (TypeError, ValueError):
                source_port = 0
            self._send_json({"ok": attach_window(source_port)})
            return True
        """POST 桌面模式专用：心跳 / 关闭信号 / 退 的路由（逐字搬自 do_POST）。"""
        if self._handle_desktop_route(path):
            return True
        return False
