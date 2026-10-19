# -*- coding: utf-8 -*-
"""
api/static_files.py —— 静态文件：首页 / /static/* / /lib.js

【这个文件是什么】
从 server.py 的 Handler 里按域拆出来的路由（2026-09-22）。每个方法对应一个 HTTP 方法，
方法体里的分支**逐字搬自原来的 do_GET / do_POST / do_PUT / do_DELETE**，
唯一改动是把「分支体里的裸 return」写成了 return True（表示这条请求我处理完了）。

【约定】
- 方法签名固定 (self, path)，命中并已响应返回 True，没命中返回 False；
- 调用顺序由 server.py 的 do_* 决定；域内分支顺序 = 原来文件里的顺序，不许乱；
- 这里用到的 server 里的函数/常量，靠文件底部那句 from server import ... 拿。
"""
import mimetypes
import os

class StaticRoutes:
    def get_static(self, path):
        """GET 静态文件：首页 / /static/*  的路由（逐字搬自 do_GET）。"""
        from server import WEB_DIR, urlparse
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
            return True
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            safe = os.path.normpath(rel).replace("\\", "/")
            if safe.startswith("..") or os.path.isabs(safe):
                self._send_json({"error": "禁止访问"}, 403)
                return True
            guessed = mimetypes.guess_type(safe)[0] or "application/octet-stream"
            if guessed.startswith("text/") or guessed in ("application/javascript",):
                guessed += "; charset=utf-8"
            self._serve_file(os.path.join(WEB_DIR, safe), guessed)
            return True
        if path == "/lib.js":
            self._serve_file(os.path.join(WEB_DIR, "lib.js"), "application/javascript; charset=utf-8")
            return True
        return False
