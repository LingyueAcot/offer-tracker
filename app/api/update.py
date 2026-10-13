"""这台电脑更新路由；只依赖更新服务，不依赖 server 的导入时状态。

server 最小接线（约六行）：导入 UpdateRoutes，加入 Handler 的 mixin，注册两条链；
启动时 configure(APP_VERSION, paths.program_dir()) 并 maybe_auto_check()。
services/state.py 可在构建状态时附加 update_service.status()（约两行）。
web/index.html（约四十行）增加状态、版本说明、检查/下载/安装/忽略按钮与轮询；
安装响应 close_window 为真时允许关闭窗口，再调用现有外壳关闭动作。
不确定：清单下载是否支持空 expected_sha256；回调参数及 updater 的退出等待
协议需接线方确认。spawn 接口按 subprocess.Popen，notify 接收事件字典。
"""

import json

from services import update_service


class UpdateRoutes:
    def get_update(self, path):
        if path == "/api/update/status":
            try:
                result = update_service.status()
            except Exception:
                result = {"ok": False, "message": "无法读取更新状态，请稍后重试。",
                          "http_status": 503}
            self._update_response(result)
            return True
        return False

    def post_update(self, path, payload):
        # ⚠️ 必须写**一条一条的正向判断**，不能写 `if path not in {...}: return False`：
        #    项目的路由门（tools/test_routes.py）检查"各条路由的条件互不重叠"，
        #    否定式条件会被判成"命中所有 POST 路径"，直接报红（2026-10-11 踩到）。
        if path == "/api/update/check":
            return self._update_post(path, payload)
        if path == "/api/update/download":
            return self._update_post(path, payload)
        if path == "/api/update/install":
            return self._update_post(path, payload)
        if path == "/api/update/ignore":
            return self._update_post(path, payload)
        return False

    def _update_post(self, path, payload):
        if not isinstance(payload, dict):
            self._update_response({"ok": False, "message": "请求内容应为 JSON 对象。",
                                   "http_status": 400})
            return True
        try:
            if path == "/api/update/check":
                result = update_service.check(manual=True)
            elif path == "/api/update/download":
                result = update_service.start_download()
            elif path == "/api/update/install":
                result = update_service.install()
            elif path == "/api/update/ignore":
                version = payload.get("version")
                if not isinstance(version, str):
                    result = {"ok": False, "message": "请提供要忽略的版本号。",
                              "http_status": 400}
                else:
                    result = update_service.ignore(version)
        except Exception:
            result = {"ok": False, "message": "更新操作失败，请稍后重试。",
                      "http_status": 503}
        self._update_response(result)
        return True

    def _update_response(self, result):
        """直接使用标准 HTTP Handler 接口，不猜测项目的 JSON 辅助函数。"""
        body = dict(result)
        code = body.pop("http_status", 200)
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)
