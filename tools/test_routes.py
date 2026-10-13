# -*- coding: utf-8 -*-
"""
HTTP 路由自检 —— 清点 + 逐条扫描（在**临时副本**上跑，不碰真实数据）。

用法：python tools\test_routes.py            # 在系统临时目录里造副本
      python tools\test_routes.py <空目录>    # 指定试验场（受限环境里 mkdtemp 建不出子目录时用）

【为什么有它】
2026-09-22 把 Handler 里 35 条路由按域拆到了 app/api/（见 api/ 里各模块的说明）。
（注：现在实际是 **39** 条 —— 拆分之后又加了 `/api/diagnostics`、`/api/open_logs`、
`/api/profile` 的 PUT 等几条，`GOLDEN` 清单以那个为准。）
拆路由最容易出的错是「搬丢一条 / 搬重一条 / 顺序错」，而这类错在界面上可能只表现为
某个不常用的按钮没反应。所以这里做两件事：

  1. **清点**：把当前代码里注册的路由逐条抽出来，和拆分前那份基准**逐字比对**
     （方法、条件、正则模式三元组，顺序也要一致）。
  2. **扫描**：起一个临时实例（自己的空库、端口 8767），把每条路由都访问一遍，
     断言它「不是被路由表漏掉的 404」（响应里的错误语不是「接口不存在」）。

⚠️ **清点有个天生的盲区（2026-09-27 踩到）**：抽路由用的是 `getattr(Handler, 名字)`
和 `inspect.getsource()`，所以**只看得见活下来的那个定义**。当时 `api/talks.py` 里
`put_talks` 被写了两遍，第一个定义（`PUT /api/profile` 保存画像）被第二个覆盖成死代码，
而 GOLDEN 里本来也没有这条 → 两头都缺同一条 → 这里照样报「不重不漏」全绿，
界面上的「保存画像」却永远 404。现在有两道补丁：
  · `tools/test_architecture.py` 的 **R10**（同类里不许有同名定义）拦住"写两遍"；
  · 下面第 3 步**真的存一次画像**，验证这条路由不是"在清单里但存不进去"。

唯一跳过的是 POST /api/sync_talks —— 它会真的去抓学校就业网站，不在自检里做这件事。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8")
sys.dont_write_bytecode = True

APP_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

# 拆分前（2026-09-22 server.py.before_chain_*）的完整路由清单：(HTTP 方法, 条件, 正则模式)
GOLDEN = [
    ('GET', "path in ('/', '/index.html')", ''),
    ('GET', "path.startswith('/static/')", ''),
    ('GET', "path == '/lib.js'", ''),
    ('GET', "path == '/api/state'", ''),
    ('GET', "path == '/api/ping'", ''),
    ('GET', 'self._handle_desktop_route(path)', ''),
    ('GET', "path == '/api/changelog'", ''),
    ('GET', "path == '/api/schema'", ''),
    ('GET', "path == '/api/sync_status'", ''),
    ('GET', "path == '/api/research_task'", ''),
    ('GET', "path == '/api/profile'", ''),
    ('GET', "path == '/api/diagnostics'", ''),
    ('GET', "path == '/api/open_logs'", ''),
    ('GET', "path == '/api/template.xlsx'", ''),
    ('GET', "path == '/api/ai_prompt'", ''),
    ('GET', "path == '/api/export.json'", ''),
    ('GET', "path == '/api/export.csv'", ''),
    # 简历（2026-10-10 加，配合「简历编辑 + PDF 导出」）
    ('GET', "path == '/api/resumes'", ''),
    ('GET', 'match', r"^/api/resumes/(\d+)/export[.]pptx$"),
    ('GET', 'match', r"^/api/resumes/(\d+)$"),
    ('POST', "path == '/api/attach'", ''),
    ('POST', 'self._handle_desktop_route(path)', ''),
    ('POST', 'match', r"^/api/applications/(\d+)/split$"),
    ('POST', "path == '/api/applications'", ''),
    ('POST', "path == '/api/events'", ''),
    ('POST', "path == '/api/stages'", ''),
    ('POST', "path == '/api/notes'", ''),
    ('POST', "path == '/api/talks'", ''),
    ('POST', "path == '/api/restore'", ''),
    ('POST', "path == '/api/import'", ''),
    ('POST', "path == '/api/import_excel'", ''),
    ('POST', "path == '/api/sync_talks'", ''),
    ('POST', "path == '/api/import_research'", ''),
    # 简历：新建 / 复制 / 设为默认
    ('POST', "path == '/api/resumes'", ''),
    ('POST', 'match', r"^/api/resumes/(\d+)/duplicate$"),
    ('POST', 'match', r"^/api/resumes/(\d+)/default$"),
    # 内置更新器（2G，A 档：后台一天一次 + 用户点确认才装）
    ('GET', "path == '/api/update/status'", ''),
    ('POST', "path == '/api/update/check'", ''),
    ('POST', "path == '/api/update/download'", ''),
    ('POST', "path == '/api/update/install'", ''),
    ('POST', "path == '/api/update/ignore'", ''),
    ('PUT', 'match', r"^/api/applications/(\d+)$"),
    ('PUT', 'match', r"^/api/events/(\d+)$"),
    ('PUT', "path == '/api/profile'", ''),          # 保存个人画像（2026-09-25 加，2026-09-27 才真的接上）
    ('PUT', 'match', r"^/api/talks/(\d+)$"),
    ('PUT', 'match', r"^/api/notes/(\d+)$"),
    ('PUT', 'match', r"^/api/resumes/(\d+)$"),
    ('DELETE', 'match', r"^/api/applications/(\d+)$"),
    ('DELETE', 'match', r"^/api/events/(\d+)$"),
    ('DELETE', 'match', r"^/api/stages/(\d+)$"),
    ('DELETE', 'match', r"^/api/talks/(\d+)$"),
    ('DELETE', 'match', r"^/api/notes/(\d+)$"),
    ('DELETE', 'match', r"^/api/resumes/(\d+)$"),
]

CHAINS = {"GET": "GET_CHAIN", "POST": "POST_CHAIN", "PUT": "PUT_CHAIN", "DELETE": "DELETE_CHAIN"}
BLOCKED = {("POST", "/api/sync_talks")}      # 会真的去抓学校网站，不自检

ok = True


def check(label, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("[PASS] " if cond else "[FAIL] ") + label + ("  " + extra if extra else ""))


# ---------------------------------------------------------------- 1) 清点
scratch = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="routes_test_")
if not os.path.isdir(scratch):
    os.makedirs(scratch)
print("试验场：%s\n" % scratch)
tmp = os.path.join(scratch, "app")
shutil.copytree(APP_SRC, tmp, ignore=shutil.ignore_patterns(
    "browser_profile", "webview_profile", "logs", "data", "__pycache__"))
with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as handle:
    handle.write(json.dumps({"port": 8767, "desktop": True}, ensure_ascii=False))
# ⚠️ 2026-10-11：**必须**把数据目录显式圈进试验场。副本里没有 data\，而 app/paths.py 的判据是
#    "隔壁已有库→原地，否则→%LOCALAPPDATA%" —— 不设这个变量，这份副本会被当成"新装"，
#    于是自检会往**用户真实的数据目录**里建库写数据（这正是"自检绝不许碰真数据"要防的事）。
os.environ["RESUME_TRACKER_DATA_DIR"] = os.path.join(scratch, "data")
sys.path.insert(0, tmp)
import server  # noqa: E402

import ast
import inspect


def routes_of(func):
    """从一个域方法里抽出它负责的路由：(条件, 正则模式)。只看**方法体第一层**的 if。"""
    src = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(src)
    body = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)).body
    out, pending = [], None
    for st in body:
        if isinstance(st, ast.Assign):
            v = st.value
            if isinstance(v, ast.Call) and getattr(v.func, "attr", "") == "match":
                pending = ast.get_source_segment(src, v.args[0])
            continue
        if isinstance(st, ast.If):
            cond = ast.get_source_segment(src, st.test).strip()
            if cond == "payload is None":
                pending = None
                continue
            if cond == "match":
                pat = (pending or "").strip()
                if pat[:1] in ("r", "R"):
                    pat = pat[1:]
                pat = pat.strip(chr(34)).strip(chr(39))
                out.append(("match", pat))
            else:
                out.append((cond.replace(chr(34), chr(39)), ""))
            pending = None
    return out


found = []
for method, chain_name in CHAINS.items():
    # do_POST 里桌面路由是**显式**先调的（它不在 POST_CHAIN 里），扫描时要带上
    func_names = list(getattr(server, chain_name))
    if method == "POST":
        func_names = ["post_desktop"] + func_names
    for func_name in func_names:
        fn = getattr(server.Handler, func_name, None)
        if fn is None:
            check("域方法 %s 存在" % func_name, False)
            continue
        for cond, pat in routes_of(fn):
            found.append((method, cond, pat))

# 比对用集合：拆分后同一域的分支是连在一起的，跨域顺序按「域链」重排过（见 server.py 的说明）。
# 顺序不影响行为的前提是「各域条件互斥」—— 下面那条检查会逐条证明它。
check("路由清单与拆分前一致（%d 条，不重不漏）" % len(GOLDEN), sorted(found) == sorted(GOLDEN),
      "" if sorted(found) == sorted(GOLDEN) else "多了 %s；少了 %s" % (
          [f for f in found if f not in GOLDEN], [g for g in GOLDEN if g not in found]))


# ---------------------------------------------------------------- 2) 扫描
server.init_db()
httpd = ThreadingHTTPServer(("127.0.0.1", 8767), server.Handler)
server._SHUTDOWN["fn"] = httpd.shutdown
server._ALIVE["beat"] = time.time()
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.4)
BASE = "http://127.0.0.1:8767"


def call(method, path, payload=None):
    data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def probe_path(cond, pattern):
    """从条件里推出一个能命中的具体路径。"""
    if "desktop_route" in cond:
        return "/api/heartbeat"          # 桌面那三个接口（心跳/关闭/退出）之一
    if pattern:
        # 2026-10-11：正则里的转义与字符类也要还原成路径字面量，否则带点的路由
        # （如 /api/resumes/1/export.pptx）会被探针漏掉、误报"命中 0 条"。
        return (pattern.strip('r"\'')
                .replace("^", "").replace("$", "")
                .replace("(\\d+)", "999")
                .replace("\\.", ".").replace("[.]", "."))
    m = re.search(r"'([^']+)'", cond)
    if "startswith('/static/')" in cond:
        return "/static/app.ico"
    if "path in" in cond:
        return "/"
    return m.group(1) if m else "/"


def matches(cond, pattern, path):
    """这条路由的条件能不能命中这个路径。"""
    if pattern:
        return re.match(pattern, path) is not None
    if "desktop_route" in cond:
        return path in ("/api/heartbeat", "/api/closing", "/api/shutdown")
    try:
        return bool(eval(cond, {"path": path, "__builtins__": {}}))
    except Exception:
        return False


overlap = []
for method in CHAINS:
    routes = [(c, p) for m, c, p in GOLDEN if m == method]
    for cond, pattern in routes:
        probe = probe_path(cond, pattern)
        hits = [c for c, p in routes if matches(c, p, probe)]
        if len(hits) != 1:
            overlap.append("%s %s 命中 %d 条" % (method, probe, len(hits)))
check("各条路由的条件互不重叠（所以域链的顺序不影响行为）", not overlap, "；".join(overlap))


missing = []
for method, cond, pattern in GOLDEN:
    path = probe_path(cond, pattern)
    if (method, path) in BLOCKED:
        continue
    if method == "GET":
        status, body = call("GET", path)
    else:
        status, body = call(method, path, {})
    routed = not (status == 404 and "接口不存在" in body)
    if not routed:
        missing.append("%s %s" % (method, path))
check("每条路由都能被路由表命中（%d 条，跳过 %d 条）"
      % (len(GOLDEN) - len(BLOCKED), len(BLOCKED)), not missing,
      "被漏掉的：" + "、".join(missing) if missing else "")

status, body = call("GET", "/api/nope")
check("未知路径仍然给「接口不存在」404", status == 404 and "接口不存在" in body, "HTTP %d" % status)
status, body = call("POST", "/api/nope", {})
check("未知 POST 路径也是 404", status == 404 and "接口不存在" in body, "HTTP %d" % status)

# ---------------------------------------------------------------- 3) 两个"光有路由不算数"的检查
# 为什么加（2026-09-27）：路由清单对不上只是**一种**坏法。当时 `PUT /api/profile` 是
# 「清单里没有、实际也 404」，但如果哪天它变成「清单里有、却存不进去」，上面两步同样测不出来。
# 所以这里把"用户真的会做的那一下"跑一遍：填个主专业 -> 存 -> 看文件里有没有。
status, body = call("PUT", "/api/profile",
                    {"major_primary": "__自检专业__", "city_keywords": ["__自检城市__"]})
profile_path = os.path.join(tmp, "个人画像.json")
profile_text = open(profile_path, encoding="utf-8").read() if os.path.isfile(profile_path) else ""
check("PUT /api/profile 真的把画像写进文件（不是只回 200）",
      status == 200 and "__自检专业__" in profile_text,
      "HTTP %d；画像文件里%s" % (status, "有" if "__自检专业__" in profile_text else "没有"))

# 畸形负载**不许**变成 500：入口该回 400 人话错误，而不是"后台出错"。
# 这一组是 2026-09-27 实测过的真崩溃路径（restore 9 组 / import_research 3 组全部 500）。
LOUD = [
    ("POST", "/api/restore", {"application": "x"}),
    ("POST", "/api/restore", {"application": 7}),
    ("POST", "/api/restore", {"application": {"company": "c"}, "events": "x"}),
    ("POST", "/api/restore", {"application": {"company": "c"}, "events": 5}),
    ("POST", "/api/restore", {"application": {"company": "c"}, "events": [None]}),
    ("POST", "/api/restore", {"application": {"company": "c"}, "events": ["x"]}),
    ("POST", "/api/import_research", {"text": {"a": 1}}),
    ("POST", "/api/import_research", {"text": 123}),
    ("POST", "/api/import_research", {"text": ["x"]}),
]
loud = []
for method, path, payload in LOUD:
    status, body = call(method, path, payload)
    if status >= 500 or status < 0:
        loud.append("%s %s %s -> %s" % (method, path, json.dumps(payload, ensure_ascii=False), status))
check("畸形负载不许 500（%d 组）" % len(LOUD), not loud, "；".join(loud))

# 顺带确认桌面模式那三个接口没被拆坏（它们不走 /api/ 的常规分支）
check("心跳 / 关闭信号仍是 200", call("POST", "/api/heartbeat")[0] == 200
      and call("POST", "/api/closing?shport=1")[0] == 200)

try:
    httpd.shutdown()
except Exception:
    pass
shutil.rmtree(scratch, ignore_errors=True)
print("\n结果：" + ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
