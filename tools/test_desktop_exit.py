# 用一个临时副本单独跑一遍「关掉窗口 → 程序自动退出」，
# 不动正在运行的那个实例（免得把用户看到的窗口弄没）。
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8")

# 副本必须来自**当前在用的 app/**，所以从本文件位置推算（tools/ 的上一级就是项目根）。
#
# 这里原来写死的是 2026-09-09 生成时留下的旧快照路径：
#     %USERPROFILE%\Documents\外部评审\2026-09-09\zuo\outputs\应届海投管理_桌面版
# 那份快照的 launcher.py 是 12.6 KB、现在是 61.7 KB，index.html 也不一样了
# （只有 server.py 碰巧一致，所以很久没暴露）。它照样会报「全部通过」，验的却是旧代码 ——
# 这种静默失效比直接报错更坑，改成从脚本位置推算就不会再走偏。
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if not os.path.isfile(os.path.join(SRC, "server.py")):
    print("找不到程序本体（%s）。" % SRC)
    print("这个脚本要放在 tools\\ 里、和 app\\ 并排（见 tools\\README.md）。")
    sys.exit(2)

tmp = os.path.join(tempfile.mkdtemp(prefix="desktop_exit_"), "app")
# 只复制要跑的东西：
#   browser_profile / webview_profile —— 被正在运行的实例占着，复制不了也不需要
#   logs / data —— 运行时产物；data 由 init_db() 自己建，不用把用户的数据库搬过来
shutil.copytree(SRC, tmp, ignore=shutil.ignore_patterns(
    "browser_profile", "webview_profile", "logs", "data", "__pycache__"))
# 换个端口，避免和正在跑的实例打架
with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as handle:
    handle.write(json.dumps({"port": 8767, "desktop": True}, ensure_ascii=False))

# ⚠️ 2026-10-11：显式把数据目录圈进试验场。副本里没有 data\，而 app/paths.py 的判据是
#    "隔壁已有库→原地，否则→%LOCALAPPDATA%"；不设这个变量，这份副本会被当成"新装"，
#    于是自检会往**用户真实的数据目录**里建库（自检碰真数据是红线）。
os.environ["RESUME_TRACKER_DATA_DIR"] = os.path.join(os.path.dirname(tmp), "data")

sys.path.insert(0, tmp)
import server  # noqa: E402

PORT = 8767
# 模拟启动器把外壳控制端口传进来（真实运行时是 launcher.py 用 --shell-port 传的）。
# 有了它，只有「窗口那个页面」发的关闭信号才算数——别的页面（浏览器里瞄一眼、
# 无界面浏览器抓页面做自检）发的一律忽略，否则它们一关就把正在用的程序带走。
#
# ⚠️ 2026-09-24 改：这个值现在住在 `services/lifecycle.py`（`touch_closing` 要用它，
# 而 services/ 不许 import server）。所以**必须用 set_shell_port() 注入**——
# 原来那句 `server.SHELL_PORT = 4711` 只改 server 里的一个别名，
# 改不到真正读的那一份，会让下面 4 条来源校验**静默失灵**（测试照样"全过"）。
SHELL_PORT = 4711
from services import lifecycle as _lifecycle  # noqa: E402
_lifecycle.set_shell_port(SHELL_PORT)
server.init_db()
httpd = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
server._SHUTDOWN["fn"] = httpd.shutdown
server._ALIVE["beat"] = time.time()
threading.Thread(target=httpd.serve_forever, daemon=True).start()
threading.Thread(target=server.desktop_watchdog, daemon=True).start()
time.sleep(0.5)

ok = True


def check(label, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("[PASS] " if cond else "[FAIL] ") + label + ("  " + extra if extra else ""))


def post(path):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path), data=b"{}", method="POST"
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.status


check("心跳接口返回 200", post("/api/heartbeat?shport=%d" % SHELL_PORT) == 200)

# 快速重开：接管后旧窗口的迟到信号必须失效，忙碌任务不受影响。
import launcher
_lifecycle.touch_closing(SHELL_PORT)
_lifecycle.add_busy(1)
check("新窗口握手接管待退出后端", launcher.attach_backend(PORT, SHELL_PORT + 2))
check("接管取消倒计时且保留后台任务", not _lifecycle.ALIVE["closing"] and _lifecycle.ALIVE["busy"] == 1)
check("旧窗口关闭不能关闭新窗口", not _lifecycle.touch_closing(SHELL_PORT))
check("旧窗口心跳不能续命", not _lifecycle.touch_heartbeat(SHELL_PORT))
check("新窗口心跳被接受", _lifecycle.touch_heartbeat(SHELL_PORT + 2))
with _lifecycle.STATE_LOCK:
    _lifecycle.ALIVE["done"] = True
check("退出闩锁已落下时拒绝接管", not launcher.attach_backend(PORT, SHELL_PORT))
with _lifecycle.STATE_LOCK:
    _lifecycle.ALIVE["done"] = False
_lifecycle.add_busy(-1)
check("恢复当前测试窗口归属", launcher.attach_backend(PORT, SHELL_PORT))

# 场景一：**别的页面**发的关闭信号（不带 shport）不算数 —— 等过一个宽限期，程序不能退
status = post("/api/closing")
time.sleep(server.CLOSE_GRACE_SECONDS + 3)
check("不带外壳端口的关闭信号被忽略（别的页面关掉不影响程序）",
      not server._ALIVE["done"], "HTTP %d" % status)
check("被忽略时也不会记成「正在关闭」", not server._ALIVE["closing"])

# 场景二：端口不对，同样不算数
post("/api/closing?shport=%d" % (SHELL_PORT + 1))
time.sleep(server.CLOSE_GRACE_SECONDS + 3)
check("端口不对的关闭信号也被忽略", not server._ALIVE["done"])

# 场景三：窗口页面刷新（带对端口的关闭信号 + 随后又来了心跳）—— 不应该退出
post("/api/closing?shport=%d" % SHELL_PORT)
time.sleep(0.5)
post("/api/heartbeat?shport=%d" % SHELL_PORT)
time.sleep(server.CLOSE_GRACE_SECONDS + 3)
check("刷新页面不会误退出", not server._ALIVE["done"])
# 场景四：**有后台任务在跑的时候**，带对端口的关闭信号也不该把程序带走。
# 【为什么这么验】原来想直接起一个真同步来验，但实测两次都崩：同步一跑起来（要拉 Chrome
#   去抓学校网站），同一个进程里的 HTTP 就慢到 5 秒没响应，测试自己先超时挂了 ——
#   这样验不出结论，只会得到一个吓人的 traceback。
#   所以改成**直接验那个闸门**：服务端靠 _ALIVE["busy"] 计数挡住看门狗（同步进来的那行
#   是 start_sync_async 里的 add_busy(1)）。这里手动把计数拉起来，验「忙的时候关窗口不退」、
#   「忙完再关就退」这两半 —— 确定、不依赖网络，也正好是这条规则的全部逻辑。
check("没后台任务时，busy 计数是 0", server._ALIVE["busy"] == 0,
      "busy=%d" % server._ALIVE["busy"])
server.add_busy(1)
post("/api/closing?shport=%d" % SHELL_PORT)
time.sleep(server.CLOSE_GRACE_SECONDS + 3)
check("有后台任务在跑时，正确的关闭信号也不会把程序带走",
      not server._ALIVE["done"] and server._ALIVE["busy"] > 0,
      "busy=%d done=%s" % (server._ALIVE["busy"], server._ALIVE["done"]))
server.add_busy(-1)
check("后台任务做完后计数归零", server._ALIVE["busy"] == 0)

# 场景五：窗口页面真的关掉了（带对端口，之后再没有心跳）—— 应该自动退出
post("/api/closing?shport=%d" % SHELL_PORT)
print("   已发关闭信号，等 %.0f 秒看它退不退…" % (server.CLOSE_GRACE_SECONDS + 6))
time.sleep(server.CLOSE_GRACE_SECONDS + 6)
check("关掉窗口后自动退出", server._ALIVE["done"])

print("\n结果：" + ("全部通过" if ok else "存在失败项"))
try:
    httpd.shutdown()
except Exception:
    pass
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if ok else 1)
