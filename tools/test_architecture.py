# -*- coding: utf-8 -*-
"""
架构依赖方向的自动检查 —— 纯逻辑，不用开程序，也不碰真实数据。

用法：
    python tools\\test_architecture.py

【为什么有它】
`docs/architecture` 那套「谁负责什么、谁能依赖谁」原来只写在文档里和 code review 的脑子里。
文档会过期、脑子会漏，而这个项目已经吃过一次亏：`core/` 的边界一直靠一条 AST 检查盯着
（`test_core.py`），所以它至今 0 违规；而 `api/` 和 `store` 的边界**没有任何检查**，
于是 `api/` 七个模块全都反向依赖了 `server`，谁也没发现。

这个脚本把那些规矩变成**可执行的**：违规就红，红就别提交。

【两类规则，分开报】
- **BLOCKING（必须全绿）**：现在就守得住的规矩。它红了就是真违规，exit 1。
- **WATCH（已知欠账）**：现在**确实违规**、但已经排进计划要修的（见
  `docs/架构.md` 的 S5）。它们只报告、不让脚本失败 —— 因为
  「假装没这事」和「一直红着没人看」都会让检查变成摆设。等 S5 做完，
  把对应条目从 WATCH 挪到 BLOCKING 即可（脚本里有说明）。

【一句话判据（照 sqlite-utils 的做法）】
依赖方向必须是单向的：越靠下越纯，越靠下越不知道上面的事。

        server.py  →  api/  ────────▶  store.py
                          └────────▶  core/  →  只标准库

【怎么跑它自己】
改完 `app/` 里的模块结构要跑它；新增一个模块（比如以后的 `services/`）时，
把新模块加进下面的 `PACKAGES` 并想清楚它该在哪一层。
"""
import ast
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

ok = True
watch_hits = []


def check(label, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("[PASS] " if cond else "[FAIL] ") + label + ("  " + extra if extra else ""))


def watch(label, cond, extra=""):
    """已知欠账：不通过也不让脚本失败，但要显式报出来，别让它悄悄消失。"""
    if cond:
        print("[WATCH] " + label + "  （已修正 —— 可以把它挪进 BLOCKING 了）")
    else:
        watch_hits.append(label)
        print("[WATCH] " + label + ("  " + extra if extra else ""))


# --------------------------------------------------------------------------- #
# 读源码 + 取出真正的 import（用 AST，不搜字符串）
#
# ⚠️ 第一版 store 的依赖检查就是搜字符串写的，结果把自己的说明文字
#    「不 import server」当成了违规。所以这里一律用 AST。
# --------------------------------------------------------------------------- #
def module_path(rel):
    return os.path.join(APP_DIR, rel)


def imports_of(rel):
    """
    返回这个模块**所有** import 的顶层包名（含函数内 import —— 那些同样算依赖）。
    还要区分「模块级」和「函数内」：api/ 现在靠函数内 import 绕循环导入，
    这本身是一种依赖，只是形式不同，报告里要能看出来。

    ⚠️ 判断「函数内」必须真的去走函数**体**，不能只 `hasattr(node, "lineno")` ——
    `lineno` 是所有语句节点都有的属性（Python 3.8+），那样写会把整个模块都算成
    「函数内」，于是所有模块级 import 都被误报成函数内。（第一版就是这么错的。）
    """
    src = open(module_path(rel), encoding="utf-8").read()
    tree = ast.parse(src)
    top_level, in_function = set(), set()

    # 只走「函数/方法体」内部，收集里面的 import
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in node.body:
            for inner in ast.walk(sub):
                if isinstance(inner, ast.Import):
                    for a in inner.names:
                        in_function.add(a.name.split(".")[0])
                elif isinstance(inner, ast.ImportFrom):
                    if inner.module:
                        in_function.add(inner.module.split(".")[0])

    # 整个文件扫一遍，减去函数内的，就是模块级的
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top_level.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_level.add(node.module.split(".")[0])

    top_level -= in_function
    return top_level, in_function


def sqlite_connects(rel):
    """
    精确找出 `sqlite3.connect(...)` —— 按 AST 的**限定名**判断，
    不看裸方法名。第一版用「有没有调用名叫 connect 的东西」来判断，
    于是 `launcher.py` 里的 `sock.connect(...)`（那是网络连接！）被误报成连库。
    """
    tree = ast.parse(open(module_path(rel), encoding="utf-8").read())
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "connect":
            v = f.value
            if isinstance(v, ast.Name) and v.id == "sqlite3":
                hits.append("sqlite3.connect")
            elif isinstance(v, ast.Attribute) and v.attr == "sqlite3":
                hits.append("...sqlite3.connect")
    return hits


def calls_of(rel, names):
    """找出代码里真正**调用**了哪些指定名字（含 `os.open` 这种属性调用）。"""
    tree = ast.parse(open(module_path(rel), encoding="utf-8").read())
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id in names:
            found.add(f.id)
        elif isinstance(f, ast.Attribute) and f.attr in names:
            found.add(f.attr)
    return found


def all_imports(rel):
    top, inner = imports_of(rel)
    return top | inner


def sql_text_of(rel):
    """把所有字符串常量拼起来（用来找 CREATE TABLE / ALTER TABLE 这类 SQL）。"""
    tree = ast.parse(open(module_path(rel), encoding="utf-8").read())
    return "\n".join(n.value for n in ast.walk(tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str))


def py_files(folder):
    base = os.path.join(APP_DIR, folder)
    return sorted(f for f in os.listdir(base) if f.endswith(".py"))


# --------------------------------------------------------------------------- #
# 分层定义
# --------------------------------------------------------------------------- #
CORE_MODULES = ["core/" + f for f in py_files("core")]
API_MODULES = ["api/" + f for f in py_files("api")]
ENTRY_MODULES = ["server.py", "launcher.py"]
INFRA_MODULES = ["store.py", "schema.py", "backup.py", "winenv.py"]

print("被检查的模块：")
print("  core/      %d 个" % len(CORE_MODULES))
print("  api/       %d 个" % len(API_MODULES))
print("  入口       %s" % "、".join(ENTRY_MODULES))
print("  基础设施   %s" % "、".join(INFRA_MODULES))
print()

# --------------------------------------------------------------------------- #
# 规则 R1–R3：core/ 是最内层，只许标准库
# --------------------------------------------------------------------------- #
print("--- R1 core/ 不许依赖本项目任何上层模块 ---")
UPPER = {"server", "api", "store", "backup", "schema", "launcher", "services"}
bad = {}
for rel in CORE_MODULES:
    hit = all_imports(rel) & UPPER
    if hit:
        bad[rel] = sorted(hit)
check("core/ 不 import server / api / store / backup / schema / launcher / services",
      not bad, str(bad) if bad else "")

print()
print("--- R2 core/ 不许 import 会碰 I/O 的标准库 ---")
IO_STDLIB = {"sqlite3", "urllib", "http", "subprocess", "socket", "threading",
             "webbrowser", "ssl", "shutil", "tempfile", "tkinter", "requests"}
bad = {}
for rel in CORE_MODULES:
    hit = all_imports(rel) & IO_STDLIB
    if hit:
        bad[rel] = sorted(hit)
check("core/ 不 import sqlite3 / urllib / http / subprocess / socket / threading / …",
      not bad, str(bad) if bad else "")

print()
print("--- R3 core/ 不许 import os / io / json / pathlib（间接碰文件的口子） ---")
# 这一条是**补网眼**：test_core.py 原来只盯着 open() 和几个明显的 I/O 库，
# 而 os / io / json / pathlib 全都能拿来读写文件，却一个都没在禁名单里。
FILE_STDLIB = {"os", "io", "json", "pathlib"}
bad = {}
for rel in CORE_MODULES:
    hit = all_imports(rel) & FILE_STDLIB
    if hit:
        bad[rel] = sorted(hit)
check("core/ 不 import os / io / json / pathlib", not bad, str(bad) if bad else "")

print()
print("--- R4 core/ 不许调用 open() / os.open() / Path.read_text() 这类 ---")
FILE_CALLS = {"open", "read_text", "write_text", "read_bytes", "write_bytes"}
bad = {}
for rel in CORE_MODULES:
    hit = calls_of(rel, FILE_CALLS)
    if hit:
        bad[rel] = sorted(hit)
check("core/ 里没有 open() / read_text() / write_text() / …", not bad, str(bad) if bad else "")

# --------------------------------------------------------------------------- #
# R5–R6：SQL 只许从 store.py 出
# --------------------------------------------------------------------------- #
print()
print("--- R5 只有 store.py 可以 sqlite3.connect ---")
# 说明：schema.py / backup.py 也 import sqlite3，但它们**不 connect 用户库**——
# schema 拿的是传进来的连接，backup 用 sqlite3 自己的在线备份接口开的是备份文件。
# 所以这条只查「谁在 connect」，并把它们列出来让人一眼看清。
connectors = []
for rel in CORE_MODULES + API_MODULES + ENTRY_MODULES + INFRA_MODULES:
    if sqlite_connects(rel):
        connectors.append(rel)
check("sqlite3.connect 只出现在 store.py / backup.py（备份走的是另一个文件）",
      set(connectors) <= {"store.py", "backup.py"}, "实际：%s" % (connectors or "无"))

print()
print("--- R6 结构变更（CREATE/ALTER TABLE）只许在 schema.py ---")
DDL = ("CREATE TABLE", "ALTER TABLE", "DROP TABLE")
bad = {}
for rel in CORE_MODULES + API_MODULES + ENTRY_MODULES + ["store.py"]:
    text = sql_text_of(rel).upper()
    hit = [d for d in DDL if d in text]
    if hit:
        bad[rel] = hit
check("CREATE/ALTER/DROP TABLE 不出现在 schema.py 以外的地方",
      not bad, str(bad) if bad else "")

# --------------------------------------------------------------------------- #
# R7–R8：api/ 与基础设施的依赖方向
# --------------------------------------------------------------------------- #
print()
print("--- R7 api/ 不许依赖 store.py 或 backup.py/schema.py（它只该调 services/ 或 server 转出来的东西） ---")
bad = {}
for rel in API_MODULES:
    hit = all_imports(rel) & {"store", "backup", "schema", "launcher"}
    if hit:
        bad[rel] = sorted(hit)
check("api/ 不 import store / backup / schema / launcher", not bad, str(bad) if bad else "")

print()
print("--- R8 只有入口（server.py）可以 import api/ ---")
bad = {}
for rel in API_MODULES + CORE_MODULES + INFRA_MODULES:
    if "api" in all_imports(rel):
        bad[rel] = ["api"]
check("api/ 不被 api/ 或 core/ 或 store 反向依赖", not bad, str(bad) if bad else "")

# --------------------------------------------------------------------------- #
# WATCH：已知欠账（S5 要修的）
# --------------------------------------------------------------------------- #
print()
print("--- R9 api/ 里不许有**模块级** `from server import`（已在 BLOCKING） ---")
# 这一条 2026-09-24 从 WATCH 升级上来的（S4 做完 desktop.py 之后它就守住了）。
# 为什么单独拎出来：模块级依赖是**导入期**就生效的，比函数内 import 更难拆、也更容易成环；
# 函数内 import 至少还把依赖藏在调用时。
top_level_deps = sorted(rel for rel in API_MODULES
                        if "server" in imports_of(rel)[0])
check("api/ 里没有模块级 `from server import`（依赖只许藏在函数里，不许在导入期）",
      not top_level_deps,
      "模块级依赖的：%s" % "、".join(os.path.basename(d) for d in top_level_deps))

# --------------------------------------------------------------------------- #
# R10 同名定义不许出现两次（后一个会**静默覆盖**前一个）
# --------------------------------------------------------------------------- #
print()
print("--- R10 同一个模块 / 同一个类里不许有同名定义 ---")
# 为什么有这条（2026-09-27 加）：`api/talks.py` 里 `put_talks` 被写了两次 ——
# 第一个定义里的 `PUT /api/profile`（保存个人画像）被第二个整个覆盖，
# 接口从路由表里消失、界面点「保存画像」永远回 404；而 `test_routes.py` 抽路由用的是
# `getattr(Handler, 名字)`，只看得见活下来的那个，所以它照样报全绿。
# 这类"写了两遍"在运行期**没有任何信号**，只能靠静态检查拦。
# 只看模块体和类体的**直接子节点**：写在 if/else 两个分支里的同名定义是合法写法，不算。
def duplicate_defs(rel):
    tree = ast.parse(open(module_path(rel), encoding="utf-8").read())
    dups = []

    def scan(body, scope):
        seen = {}
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if stmt.name in seen:
                    dups.append("%s%s（第 %d 行被第 %d 行覆盖）"
                                % (scope, stmt.name, seen[stmt.name], stmt.lineno))
                else:
                    seen[stmt.name] = stmt.lineno
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                scan(stmt.body, scope + stmt.name + ".")

    scan(tree.body, "")
    return dups


ALL_PY = []
for _folder in ("core", "api", "services"):
    ALL_PY += [_folder + "/" + f for f in py_files(_folder)]
ALL_PY += sorted(f for f in os.listdir(APP_DIR) if f.endswith(".py"))
bad = {}
for rel in ALL_PY:
    hit = duplicate_defs(rel)
    if hit:
        bad[rel] = hit
check("没有同名定义被后来的静默覆盖（查了 %d 个模块）" % len(ALL_PY), not bad,
      "；".join("%s: %s" % (k, "、".join(v)) for k, v in sorted(bad.items())) if bad else "")

# --------------------------------------------------------------------------- #
# R11 winenv.py 是叶子：只许标准库，不许 import 项目里任何模块
# --------------------------------------------------------------------------- #
print()
print("--- R11 winenv.py 只 import 标准库（它要被 launcher / server / services 共用） ---")
# 为什么有这条（2026-09-27 加）：DPI 感知和显示器几何必须**只有一份实现** ——
# 之前 launcher.py 和 services/diagnostics.py 各写了一份 ctypes 采集代码，
# 于是诊断报告那份在缩放机器上永远报 96（不感知的进程拿到的是虚拟化值）。
# 合并到 winenv.py 之后，它是所有人的下游，自己不许再依赖任何人（否则要成环）。
PROJECT_MODULES = {"server", "launcher", "api", "services", "store", "schema", "backup", "core"}
winenv_all = all_imports("winenv.py")
winenv_bad = sorted(winenv_all & PROJECT_MODULES)
check("winenv.py 不 import 项目里任何模块", not winenv_bad,
      "实际 import 了：%s" % winenv_bad if winenv_bad else "")
winenv_third = sorted(winenv_all - set(sys.stdlib_module_names) - {"ctypes.wintypes"})
check("winenv.py 只用标准库（ctypes）", not winenv_third,
      "多出来的：%s" % winenv_third if winenv_third else "")

# --------------------------------------------------------------------------- #
# WATCH：已知欠账 —— **这是「登记处」，不是待办清单**
#
# 2026-09-25 说明（重要，别把这段当欠账催办）：
# 下面这条**是故意留着的**。它剩下的尾巴分三类：
#   ① 纯路径常量（WEB_DIR / CHANGELOG_PATH / AI_PROMPT_PATH / EXCEL_TEMPLATE_PATH）
#      和标准库转发（urlparse / now_str）—— 搬它们只是「把常量换个地方放」，
#      对「加新功能更容易」几乎零帮助，**决定不做**；
#   ② io 域（import_package / import_excel_package，243 行）—— 收益中等，
#      **可选**，要做就单独一轮；
#   ③ 同步域（SYNC_STATE / start_sync_async，绑着 231 行的 sync_gdut_talks）
#      —— 全项目唯一「联网 + 写真库 + 被自检跳过」的路径，**建议不做或另开一轮**。
#
# 把它留在 WATCH 而不是删掉的用处：**如实记录还剩什么**（见
# docs/架构.md 的「S4 收尾说明」）。哪天某一条真被清掉了，
# 这里会自己打印「（已修正 —— 可以把它挪进 BLOCKING 了）」，提醒把它升级成硬规矩。
# --------------------------------------------------------------------------- #
print()
print("--- WATCH：已知欠账登记（不是待办清单，见本文件注释）---")
dependents = sorted(rel for rel in API_MODULES if "server" in all_imports(rel))
watch("api/ 不 import server（现在 %d/%d 个模块依赖它）" % (len(dependents), len(API_MODULES)),
      not dependents,
      "依赖的：%s" % "、".join(os.path.basename(d) for d in dependents))

# --------------------------------------------------------------------------- #
print()
if watch_hits:
    print("已知欠账 %d 条（[WATCH] 是登记处、不是待办；说明见本文件那段注释和" % len(watch_hits))
    print("docs/架构.md 的「S4 收尾说明」）：")
    for w in watch_hits:
        print("  · " + w)
print()
print("结果：" + ("全部通过" if ok else "存在失败项"))
if watch_hits and ok:
    print("（BLOCKING 规则全绿。WATCH 是如实登记还剩什么，不计失败）")
sys.exit(0 if ok else 1)
