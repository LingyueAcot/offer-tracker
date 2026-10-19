# -*- coding: utf-8 -*-
"""
应届海投管理 —— 本地网页版

零第三方依赖：只使用 Python 标准库（http.server + sqlite3）。
数据保存在同目录 data/applications.db，关掉程序数据也不会丢。

直接运行： python server.py（桌面版由 launcher.py 拉起，双击快捷方式）
"""

# =========================================================================== #
# 【给 AI / 开发者的说明】改动这个文件前先读这里
# --------------------------------------------------------------------------- #
# 角色：后端。只用 Python 标准库（http.server + sqlite3），没有第三方依赖；
#       只有 Excel 导入这一项会可选地用到 openpyxl。
#       **纯逻辑（阶段推导 / 统计 / 宣讲会解析 / 调研解析）都在 core/ 包里**，
#       本文件只留「HTTP 路由 + 读写数据库 + 编排」。改 core/ 之前先看那个模块的顶部说明。
#
# 目录结构（都在本文件所在文件夹内）：
#   launcher.py            窗口宿主（桌面版）：起服务、开窗口、单实例、无边框补丁
#   server.py              本文件：HTTP 服务 + SQLite 数据层 + 宣讲会同步
#   backup.py              每次启动自动备份；main() 在 init_db() 之前调它
#   core/                  纯函数（可单独测，见 tools/test_core.py）
#   web/index.html         前端：单文件 HTML + CSS + JS（顶部也有给 AI 的说明）
#   data/applications.db   主数据（SQLite）
#   data/change_log.jsonl  改动日志，只追加、不覆盖，含删除前的完整数据
#   data/backups/<时间戳>/  每次启动自动留的一份备份
#   logs/                  launcher.log（启动器）、backup.log（备份）
#   config.json            可选，例如 {"port": 8770}，不写则默认 8765
#
# 数据模型：
#   applications  一行 = 一个岗位的投递记录（公司 + 岗位）
#   events        一行 = 该投递的一次进展（阶段 / 结果 / 登记时间 / 完成时间）
#   stages        常用阶段列表（预设 + 用户自定义）
#   meta          版本信息（schema_version / app_version）
#
# 关键约定（改代码时必须保持）：
#   1. events.created_at = 登记时间，登记后不允许被编辑修改。
#   2. events.completed_at = 完成时间，点绿勾或 result 改为“已完成”时写入。
#   3. 登记新阶段时，之前 result='进行中' 的阶段自动置为“已完成”（连锁反馈）。
#   4. 删除是硬删除，但删除前的完整数据会写入 change_log.jsonl。
#   5. 新增投递时，初始阶段“投递”的 result 默认是“已完成”。
#   6. 所有写操作都必须调用 log_change(...)，否则改动日志会不完整。
#   7. 前端只通过 /api/* 通信；分享可编辑版在浏览器里用 localApi() 镜像了同一套接口，
#      所以这里改接口时，web/index.html 里的 localApi 也要同步改。
#
# 新增接口的标准做法：
#   do_POST / do_PUT / do_DELETE 里加路由 -> 写对应的数据函数（记得 log_change）
#   -> 最后统一返回 build_state()，前端会用返回的 state 整体刷新。
#
# 改完务必按 开发记录 第 9 节“验证清单”自测。
# =========================================================================== #

from __future__ import print_function

import base64
import concurrent.futures
import csv
import ctypes
import io
import json
import mimetypes
import os
import re
import shutil
import socket
# 2026-09-24：`import sqlite3` 删掉了 —— 本文件不再自己连库，全部走 store.py。
# 想直接连库的代码请加在 store.py 里（那里是唯一的 SQL 出口）。
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import backup  # 同目录的「启动时自动备份」模块，说明见 backup.py 顶部
import paths as app_paths  # 同目录的「路径契约」（2026-10-11 加），说明见 paths.py 顶部
import schema  # 同目录的「建表 / 老库补字段」模块（唯一允许改表结构的地方）
import store   # 同目录的「数据库访问层」（2026-09-24 新增）：连接/事务/行映射的唯一出口
import winenv  # 同目录的「DPI 感知 + 显示器几何」（2026-09-27 新增），诊断报告要用
# 编排层（2026-09-24 新增，见 docs/架构.md 的 S4）：
# api/ 依赖这一层，不再反过来 import server —— 那条反向依赖就断了。
# ⚠️ 这里 import 的是**服务模块**（不是 from services import xxx），
#    这样 `services.notes.list_notes` 这些名字在自检里能直接替换。
from services import audit as audit_service
from services import diagnostics as diagnostics_service
from services import notes as notes_service
from services import resumes as resumes_service
from services import state as state_service
from services import apps as apps_service
from services import talks as talks_service
from services import profile as profile_service
from services import research as research_service
from services import schema_compat
from services import update_service
from services import update_txn

# 本文件仍然提供 `get_conn()` / `LOCK` 这两个老名字（都转发到 store），
# 这样 api/ 里那些函数内 `from server import ...` 的写法不用动、行为也不变。
# 逐步把调用点换成 store.db().cursor() / locked_cursor() 即可（见 docs/架构.md 的 S2）。

# 纯函数都在 core/ 里（是 2026-09-22 从本文件**逐字搬出去**的，搬的时候断言过 AST 一致），
# 这里导回来继续用原来的名字，所以下面的调用点一行都没改。
# 加新逻辑时：**能不碰 DB 的就不放在本文件**，放 core/ 才能单独测（见 tools/test_core.py）。
from core.values import _parse_excel_date, as_int, clean, now_str, today_str
# ⚠️ `derive_status` 现在本文件自己不用了（派生字段整段搬进 core/stages.py 了），
# 但**必须继续导进来**：tools/test_core.py 有一条「server 用的就是 core 里那一个对象」
# 的检查点名要 `server.derive_status`，删掉会把那条检查弄红；api/ 里也可能有 from server import。
# 它是个转发，不是第二份实现。
from core.stages import attach_derived_fields, derive_status
from core.stats import compute_stats
from core.talks import (
    CAMPUS_NOISE_PATTERNS, DETAIL_KEY_HINTS, DETAIL_NOISE_HINTS, FOOTER_CUT_HINTS,
    JOB_LIST_NOISE, RECRUIT_BRIEF_CHARS, RECRUIT_JOBS_LIMIT,
    brief_from_detail, company_key, evaluate_fit, format_job_list, html_to_lines,
    normalize_company, parse_talk_detail, parse_talk_list, parse_talk_time,
    recruit_digest, strip_noise_tail, talk_values,
)
from core.research import (
    RESEARCH_FIELDS, _fit_level_from_text, _match_talks, _merge_notes,
    _split_position_note, parse_research_text,
)
from core.talk_sync import (
    dedupe_items, items_needing_detail, match_loose, plan_cleanup,
)
from core import excel as excel_rules
from core import packaging


BASE_DIR = app_paths.program_dir()
# ⚠️ 路径**不许**在这里拼 `BASE_DIR + "data"`：全部走 `app_paths.*`（唯一决定处）。
#    判据顺序（环境变量 → config 的 data_dir → 隔壁已有库=原地 → %LOCALAPPDATA%）写在 paths.py 顶部。
DATA_DIR = app_paths.data_dir()
WEB_DIR = app_paths.web_dir()
DB_PATH = app_paths.db_path()
CHANGELOG_PATH = app_paths.changelog_path()
CONFIG_PATH = app_paths.config_path()
EXCEL_TEMPLATE_PATH = app_paths.excel_template()
# 「怎么让 AI 把自制 Excel 转成模板格式」的提示词。同一份内容也写进了模板的
# 「给 AI 的提示词」工作表里（用户拿到模板就拿到了指令），页面上的按钮直接读这个文件。
AI_PROMPT_PATH = app_paths.ai_prompt()
PROFILE_PATH = app_paths.profile_path()


def _ensure_store():
    """
    把真实路径装进 store 和 audit（只装一次）。

    为什么要有它、而且要在两个地方调：`store.py` / `services/audit.py` 刻意不自己算路径 ——
    路径的唯一定义处是本文件上面的 `DB_PATH` / `CHANGELOG_PATH`。所以得由本文件注入。
    但注入的时机有讲究：自检脚本（`test_schema.py` / `test_routes.py` /
    `test_desktop_exit.py`）是**直接 `import server` 然后立刻调 `init_db()`** 的，
    根本不会走 `main()`；只把注入写在 `main()` 里，那些脚本一跑就报「store 还没配好库路径」。

    幂等、极便宜，所以放在 `get_conn()` 和 `init_db()` 开头各调一次。
    """
    if not store.db().db_path:
        store.use(DB_PATH)
    if not audit_service.path():
        audit_service.use(CHANGELOG_PATH)
    # 版本号与「是不是桌面模式」也给 services/state.py 注进去 ——
    # 它的唯一来源仍是本文件顶部那几个常量，不在第二个地方重算。
    state_service.configure(APP_VERSION, SCHEMA_VERSION, DESKTOP_MODE,
                            detect_edition())
    if not profile_service.path():
        profile_service.use(PROFILE_PATH)


def detect_edition():
    """
    这一份是**个人版**还是**正式版**（2026-09-25 加）。

    两条路（先看显式声明，再看事实）：

    ① **显式声明**：`config.json` 里 `"build": "release"` / `"personal"`。
       这只由**发布方**在打包时写（`make_trial_package.py` 给正式版写 `release`），
       用户自己的安装里**没有这个键**，所以不会被它管住。

    ② **看事实**（没有 ① 时）：有 `个人画像.json`（你自己维护那份）或有
       `data/applications.db`（真的在用）就是**个人版**，否则是**正式版**。
       为什么要有 ②：升级时 `config.json` 是**保留用户的**（绝不覆盖），
       所以只靠 ① 的话，从老版本升上来的安装永远算不出自己是哪一版。

    ⚠️ 注意 `config.json` 里那个老的 `"edition": "desktop-v3"` **不是**这个意思
    （它是"桌面版"的发行标记），**别拿它当判断依据** —— 所以我用的键名是 `build`。

    ⚠️ 它只是给界面/排查用的**提示性信息**，程序行为一个字都不依赖它 ——
    正式版装上开始用之后显示成"个人版"也不影响任何功能（那本来就是对的）。
    """
    try:
        declared = str(CONFIG.get("build") or "").strip().lower()
        if declared in ("personal", "release"):
            return declared
    except Exception:
        pass
    try:
        if os.path.isfile(PROFILE_PATH) or os.path.isfile(DB_PATH):
            return "personal"
    except Exception:
        pass
    return "release"


def edition_label(edition=None):
    """给界面/日志看的中文名。"""
    return "个人版" if (edition or detect_edition()) == "personal" else "正式版"


HOST = "127.0.0.1"
# 2026-09-25 用户定版：1.0 是**正式版**（架构进化 + 首页顺序 + 宣讲会类别自动识别之后）。
# 1.1（2026-09-25）：个人画像改成空白模板 + 主/副专业 + 界面填写入口；新增诊断报告。
# 1.1.6（2026-10-10）：修「关窗后立刻重开 → 僵尸窗口」；1.1.7：把「探测/接管后端」提到
#   建窗口时的无边框补丁**之前**，让接管不再靠抢 15 秒宽限；1.1.8：换程序图标（用户给的图）
#   并把它嵌进程序内部 + 运行时给窗口设图标（见更新记录 38/39/40）。
# 版本号怎么涨（2026-09-25 定的规矩，改之前先看这里）：
#   修 bug / 改文案              -> 第三位 +1（1.0 -> 1.0.1）
#   新增功能 / 界面变化          -> 第二位 +1（1.0 -> 1.1）
#   动了数据库结构 / 界面大改    -> 第一位 +1（1.x -> 2.0），且此时要提醒用户：
#                                  升级包能原地升，是因为 schema.py 会在启动时给老库补字段。
APP_VERSION = "1.1.11"
# ⚠️ 这个跟 APP_VERSION 是两回事，别一起改：
#   APP_VERSION   = 给人和界面看的版本名（"1.1"），纯字符串，随便改。
#   SCHEMA_VERSION = **数据库结构的版本号**（整数）。只有 schema.py 真的改了表结构
#                    （加表/加字段）才 +1，而且已发布过的声明不许改（老库不会重跑）。
#   2026-09-24 定版 V1；2026-09-25 改名 1.0、再到 1.1 时**结构都没动**，所以这里保持 1。
SCHEMA_VERSION = 2
# 全局串行锁：现在归 store 持有（`store.db().lock`），这里保留 `LOCK` 这个老名字给
# api/ 里那些函数内 `from server import ...` 的写法用。**是同一个对象**，不是两份锁。
LOCK = store.db().lock
# 2026-09-24：`LOG_LOCK` 删掉了 —— 改动日志读写连同它的锁一起搬进了
# `services/audit.py`（那边有 `LOG_LOCK`）。全项目已无引用（server/api/tools 都查过）。

# 启动备份：main() 里跑一次，结果留在这里给 /api/state 用（逻辑都在 backup.py）
BACKUP_LOG_PATH = app_paths.backup_log_path()
# 日志目录（2026-09-25 提成常量）：诊断报告要读 logs/ 下的几个文件，
# 之前各处都是写 "logs" 字面量，多一处引用就多一个改漏的机会。
LOG_DIR = app_paths.logs_dir()

# 外壳控制端口：由 launcher.py 用 --shell-port 传进来（就是写进页面地址的 shport）。
# 用途只有一个——分辨「窗口那个页面」和「别的页面」：只有窗口页面发的关闭信号才算数。
# 0 表示没有外壳（浏览器外壳模式，或直接 python server.py），这时不校验来源。
#
# ⚠️ 2026-09-24：真正的值住在 `services/lifecycle.py`（`touch_closing` 要用它，
# 而那一层不许 import server）。这里是**别名**，下面 `from services import lifecycle`
# 之后立刻绑过去。要**改**它请调 `lifecycle.set_shell_port(port)`（`main()` 就是这么做的）——
# 直接写 `server.SHELL_PORT = N` 只会改 server 这一份、改不到真正读的那一份。
SHELL_PORT = None  # 下面从 services.lifecycle 绑过来（见那一行）
# 2026-09-24：原来这里有个 _STARTUP_BACKUP 字典存本次启动的备份结果。
# 它跟着 build_state() 一起搬进了 services/state.py（改用 set_backup() 注入）——
# 全项目已无其它引用。


def _load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


CONFIG = _load_config()
DEFAULT_PORT = int(CONFIG.get("port") or 8765)
# 同步宣讲会时往后抓几天（config.json 的 "talk_sync_days" 可以改）。
# 【为什么默认从 2 天改成 30 天】2026-09-23 用户反馈「抓下来的和学校网页上不一样」：
# 原来只抓「今天 + 后两天」，而学校列表页上是往后一个多月的场次（10-09、10-12…），
# 那些永远不会被同步进来 —— 看起来就像"没抓成功"。抓宽一点才对得上网页，
# 也不影响"页面上撤了 -> 已取消"那套判断（窗口内消失才会被标取消）。
TALK_SYNC_DAYS = int(CONFIG.get("talk_sync_days") or 30)

# --------------------------------------------------------------------------- #
# 桌面模式（config.json 里写 "desktop": true）
#   和普通网页版的区别只有三点：
#     1. 服务本身**不弹浏览器**——窗口由 launcher.py 打开（用 --app 模式，没有地址栏）；
#     2. 启动成功后把「实际端口」写进 data/runtime.json，
#        因为端口被占时程序会自己往后换，启动器得知道最终开哪个地址；
#     3. 窗口全关掉之后自动退出，不会在后台偷偷常驻。
#   退出方式有两种：页面上的「退出程序」调 /api/shutdown；或者关掉窗口后由 watchdog 收拾。
# --------------------------------------------------------------------------- #
DESKTOP_MODE = bool(CONFIG.get("desktop"))
RUNTIME_PATH = app_paths.runtime_path()
HEARTBEAT_TIMEOUT = 300.0    # 连心跳都断了这么久 -> 认为没有窗口了，可以退出（正常情况每 20 秒一次心跳）
CLOSE_GRACE_SECONDS = 15.0   # 收到「页面正在关闭」后先等这么久；期间有新心跳就取消退出（刷新页面就是这种情况）
# 桌面生命周期的状态与常量（心跳 / 关闭信号 / 看门狗 / 退出钩子）2026-09-24 搬进了
# `services/lifecycle.py` —— 因为 `api/desktop.py` 要用它们，不该反过来 import server。
# 下面这几个名字是**别名**（同一个对象 / 同一个值），老的调用点与自检脚本照旧能用：
# 改 `server.SHELL_PORT` 或 `server._ALIVE[...]` 都真的生效，因为指向的是同一个东西。
from services import lifecycle as _lifecycle  # noqa: E402

SHELL_PORT = _lifecycle.SHELL_PORT          # 别名（同一个值；改请用 set_shell_port）
HEARTBEAT_TIMEOUT = _lifecycle.HEARTBEAT_TIMEOUT
CLOSE_GRACE_SECONDS = _lifecycle.CLOSE_GRACE_SECONDS
_ALIVE = _lifecycle.ALIVE
_SHUTDOWN = _lifecycle.SHUTDOWN


def touch_heartbeat(source_port=None):
    """页面还开着：记一次心跳。实现见 `services/lifecycle.py`。"""
    return _lifecycle.touch_heartbeat(source_port)


def touch_closing(source_port=None):
    """
    页面正在关闭（beforeunload）。只认「窗口那个页面」发的信号。

    实现见 `services/lifecycle.py`（判定规则与原来逐字一致）。
    """
    return _lifecycle.touch_closing(source_port)


def add_busy(delta):
    """后台任务计数：有活儿在跑时窗口关了也不退出。实现见 `services/lifecycle.py`。"""
    return _lifecycle.add_busy(delta)


def request_shutdown():
    """优雅退出：让 serve_forever() 返回。实现见 `services/lifecycle.py`。"""
    return _lifecycle.request_shutdown()


def desktop_watchdog():
    """桌面模式看门狗。实现见 `services/lifecycle.py`（判定顺序与超时一字未改）。"""
    return _lifecycle.desktop_watchdog(DESKTOP_MODE)


def write_runtime_file(port):
    """把实际端口写给启动器（launcher.py 靠它才知道窗口该开哪个地址）。"""
    try:
        with open(RUNTIME_PATH, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"port": port, "pid": os.getpid(), "desktop": DESKTOP_MODE,
                 "started_at": now_str()},
                ensure_ascii=False,
            ))
    except Exception:
        pass


def remove_runtime_file():
    try:
        if os.path.isfile(RUNTIME_PATH):
            os.remove(RUNTIME_PATH)
    except Exception:
        pass

# 预设阶段：可以自由新增，也可以删除自定义的
PRESET_STAGES = [
    ("投递", 10),
    ("简历初筛", 20),
    ("AI测评", 30),
    ("笔试", 40),
    ("一面", 50),
    ("二面", 60),
    ("三面", 70),
    ("HR面", 80),
    ("终面", 90),
    ("Offer", 100),
    ("体检", 110),
    ("背调", 120),
    ("已拒绝", 900),
    ("已放弃", 910),
]

# 宣讲会的状态（可在前端下拉选择）
TALK_STATUS_OPTIONS = ["待参加", "已参加", "已投递", "已面试", "已放弃"]

# 宣讲会里「简历已经投出去了」的状态：到了这一步，事情就从宣讲会转移到了投递列表。
# 界面上这些场次会被收进底部的折叠区，不再占主列表；状态一到「已投递」，
# 后端还会自动在主页面生成一条投递记录（见 sync_talk_to_application）。
TALK_DONE_STATUSES = ["已投递", "已面试"]

# --------------------------------------------------------------------------- #
# 宣讲会同步（广东工业大学就业网）相关配置
# --------------------------------------------------------------------------- #
GDUT_LIST_URL = "https://career.gdut.edu.cn/teachin/index"
GDUT_VIEW_URL = "https://career.gdut.edu.cn/teachin/view/id/%s"
# 自动同步间隔（小时）：启动时距上次同步超过这个时间才自动跑一次，避免每次开程序都去抓。
# 2026-09-24 由 6 改成 24（用户要求）：6 小时太频繁 —— 一天开几次程序就会抓几次学校网站，
# 既慢又把宣讲会行的 updated_at/last_synced_at 反复刷新（那是一次真联网抓取，
# 会连着开无界面浏览器抓详情页，启动后十几秒内都在跑）。一天一次足够。
AUTO_SYNC_HOURS = 24
# 详情页渲染并发数（每条详情页要单独开一次无界面浏览器，约 5 秒）
DETAIL_WORKERS = 4
# 一次调研任务包最多列几家公司（一次给 AI 太多，质量会下降）
# 一批给 AI 几家公司。任务包里现在会附上每家公司的招聘需求原文，内容比过去厚，
# 10 家容易让 AI 敷衍后面的公司，所以降到 8 家，质量更稳。
# 2026-09-24：调研的常量搬到 services/research.py（S4）。
RESEARCH_BATCH_SIZE = research_service.RESEARCH_BATCH_SIZE
# 浏览器候选路径（找不到 Chrome 就用 Edge）
BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
# 默认个人画像（首次运行会写到 个人画像.json，之后以文件为准）
# 默认画像：实现搬到了 services/profile.py（2026-09-24，S4）。
# 这里保留名字给外部引用（是同一个对象）。
DEFAULT_PROFILE = profile_service.DEFAULT_PROFILE

# 同步状态（供前端轮询显示进度）
SYNC_STATE = {
    "running": False,
    "stage": "",
    "message": "",
    "report": None,
    "last_sync": "",
}


def log_change(action, entity, entity_id, summary, before=None, after=None):
    """
    把每一次改动追加到独立的 change_log.jsonl，包括删除前的完整数据。

    实现已搬到 `services/audit.py`（2026-09-24）。这里只是转发 ——
    因为各 service 域都要记审计，而它们**不许回头 import server**，
    所以把这一小块先搬出去，路径由 `_ensure_store()` 一起注入。
    """
    return audit_service.log_change(action, entity, entity_id, summary,
                                    before=before, after=after, now_str=now_str)


def read_changelog(limit=800):
    """读最近的改动记录，最新的在前。实现见 `services/audit.py`。"""
    return audit_service.read_changelog(limit)


def _app_dict(cur, app_id):
    row = cur.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    return dict(row) if row else None


def _event_dict(cur, event_id):
    row = cur.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return dict(row) if row else None


def _talk_dict(cur, talk_id):
    row = cur.execute("SELECT * FROM talks WHERE id=?", (talk_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# 数据库：建表 + 轻量迁移
# 说明：
#   - 用 CREATE TABLE IF NOT EXISTS 建表；
#   - 新增字段用 PRAGMA table_info 判断后再 ALTER TABLE，实现老数据自动升级；
#   - 绝对不要 DROP TABLE，否则用户数据会丢。
# --------------------------------------------------------------------------- #
def get_conn():
    """
    开一条新连接。

    ⚠️ **2026-09-24 起这是个兼容外壳**：真正的实现在 `store.py`，这里只是把
    `store.db().connect()` 转出来，好让 `api/` 里已有的 `from server import get_conn`
    和还没换过来的调用点继续能用。**新代码请用 `store.db().cursor()` /
    `locked_cursor()`** —— 它们保证把连接还回去，而 `get_conn()` 不负责关。
    """
    _ensure_store()
    return store.db().connect()


def init_db():
    """建库 / 给老库补字段。真正的结构定义在 schema.py 里（本函数只负责连接和事务）。"""
    # 数据目录不可用就**带着原因报错**，绝不悄悄换个地方建空库（见 paths.ensure_writable 的说明）。
    # 同时把原因写进"启动失败通道"（%TEMP%\ResumeTracker-startup-error.txt）：
    # 数据目录不可写时，日志目录多半也不可写，只打日志等于没打 —— 启动器能从那个文件读到原因并弹出来。
    try:
        app_paths.ensure_writable(DATA_DIR)
    except RuntimeError as err:
        app_paths.record_startup_error(str(err))
        raise
    # store 还没装路径时（比如自检脚本直接 import server）先装一下，
    # 免得 init_db() 成了「必须先调 store.use() 才能用」的隐藏前置条件。
    _ensure_store()
    # 锁 + 连接的获取/归还都交给 store：原来这里是手写 try/finally，
    # 换掉之后异常路径也一样不会漏连接（行为不变，少一处要自己操心的）。
    with store.db().locked_conn() as conn:
        # 建表 + 补字段都在 schema.py；main() 里是「先备份、再调它」，
        # 所以 ALTER 真出问题时手里还有升级前那一刻的副本。
        schema.ensure_schema(conn, PRESET_STAGES, SCHEMA_VERSION, APP_VERSION)


def build_state():
    """
    返回前端需要的完整数据（派生字段 / 统计 / 版本号 / 备份信息）。

    实现已搬到 `services/state.py`（2026-09-24，S4 的关键一步 —— 它是所有 7 个
    `api/` 模块都依赖的那个名字）。这里只是**转发**，调用点一行没改。
    """
    return state_service.build_state()


# --------------------------------------------------------------------------- #
# 写操作
# --------------------------------------------------------------------------- #
def create_application(payload):
    """
    实现已搬到 `services/apps.py`（2026-09-24，S4）。这里只是转发，调用点一行没改。


    """
    return apps_service.create_application(payload)



def update_application(app_id, payload):
    """
    实现已搬到 `services/apps.py`（2026-09-24，S4）。这里只是转发，调用点一行没改。


    """
    return apps_service.update_application(app_id, payload)



def delete_application(app_id):
    """
    实现已搬到 `services/apps.py`（2026-09-24，S4）。这里只是转发，调用点一行没改。


    """
    return apps_service.delete_application(app_id)



def split_application(app_id):
    """
    实现已搬到 `services/apps.py`（2026-09-24，S4）。这里只是转发，调用点一行没改。

    把一条“多岗位”记录拆成多条独立记录。
    每个岗位保留原来的公司、类别、投递日期，并复制一份已有的进展时间线。
    """
    return apps_service.split_application(app_id)



def create_event(payload):
    """
    实现已搬到 `services/apps.py`（2026-09-24，S4）。这里只是转发，调用点一行没改。


    """
    return apps_service.create_event(payload)



def update_event(event_id, payload):
    """
    实现已搬到 `services/apps.py`（2026-09-24，S4）。这里只是转发，调用点一行没改。


    """
    return apps_service.update_event(event_id, payload)



def delete_event(event_id):
    """
    实现已搬到 `services/apps.py`（2026-09-24，S4）。这里只是转发，调用点一行没改。


    """
    return apps_service.delete_event(event_id)



def add_stage(payload):
    """
    实现已搬到 `services/apps.py`（2026-09-24，S4）。这里只是转发，调用点一行没改。


    """
    return apps_service.add_stage(payload)



def delete_stage(stage_id):
    """
    实现已搬到 `services/apps.py`（2026-09-24，S4）。这里只是转发，调用点一行没改。


    """
    return apps_service.delete_stage(stage_id)



def sync_talk_to_application(cur, talk):
    """
    实现已搬到 `services/talks.py`（2026-09-24，S4）。这里只是转发。

    宣讲会状态变成「已投递」时，把这场宣讲会**自动同步成主页面的一条投递记录**。

    规则（都很保守，避免把手动记的数据弄乱）：
      - 只有状态是「已投递」才同步；「待参加 / 已参加」只是去过宣讲会，还不算投递；
      - 一场宣讲会只同步一次，同步出来的投递记录 id 记在 `talks.app_id`；
      - 如果投递列表里**已经有同名公司**的记录，就只把链接指过去、**不重复新建**
        （否则你按企业统计的投递数量会凭空多一家）；
      - 新建时自动登记一条「投递」进展并标成「已完成」，
        和你在主页面手动新增投递的默认状态保持一致。

    返回一个 dict 描述这次做了什么，供接口提示用；不需要同步时返回 None。
    """
    return talks_service.sync_talk_to_application(cur, talk)



def backfill_talk_applications():
    """
    实现已搬到 `services/talks.py`（2026-09-24，S4）。这里只是转发。

    开机补齐：把「状态已经是已投递、但还没同步到投递列表」的宣讲会补同步一次。

    为什么需要它：自动同步是 2026-09-20 才加上的，在那之前手动标成「已投递」的场次
    不会自己补上。这里只处理 `app_id = 0`（从没同步过）的记录——
    你要是把自动生成的那条投递记录删掉了，`app_id` 仍有值，不会被重新建出来。
    """
    return talks_service.backfill_talk_applications()



def create_talk(payload):
    """
    实现已搬到 `services/talks.py`（2026-09-24，S4）。这里只是转发。


    """
    return talks_service.create_talk(payload)



def update_talk(talk_id, payload):
    """
    实现已搬到 `services/talks.py`（2026-09-24，S4）。这里只是转发。


    """
    return talks_service.update_talk(talk_id, payload)



def delete_talk(talk_id):
    """
    实现已搬到 `services/talks.py`（2026-09-24，S4）。这里只是转发。


    """
    return talks_service.delete_talk(talk_id)



def restore_application(payload):
    """
    实现已搬到 `services/apps.py`（2026-09-24，S4）。这里只是转发，调用点一行没改。

    撤销删除：把删除前的投递 + 全部进展原样写回。
    """
    return apps_service.restore_application(payload)



def list_notes(cur=None):
    """
    读备忘录：没完成的排前面，完成的沉到后面；同组按登记时间倒序（新记的在上）。

    实现已搬到 `services/notes.py`（2026-09-24，S4 的第一个域）。
    这里只是**转发**，好让 `build_state()` 和 `api/` 原来的调用点一行都不用改。
    """
    return notes_service.list_notes(cur)


def create_note(payload):
    """新增一条备忘录。实现见 `services/notes.py`。"""
    return notes_service.create_note(payload)


def update_note(note_id, payload):
    """改一句话，或者打勾 / 取消打勾。实现见 `services/notes.py`。"""
    return notes_service.update_note(note_id, payload)


def delete_note(note_id):
    """删掉一条备忘录，删除前的内容照样进改动日志。实现见 `services/notes.py`。"""
    return notes_service.delete_note(note_id)


def export_package():
    """标准化数据包：备份、迁移、后续版本升级都用这个格式。

    组装规则在 core/packaging.py（纯函数、有测试）；这里只负责把 state 和时间戳递进去。
    """
    # 简历要**单独取完整的一份**：build_state() 里那份只有元信息（不带正文），
    # 直接拿它导出会得到一堆空壳（2026-10-10 补数据包时踩到的点）。
    return packaging.export_package(build_state(), SCHEMA_VERSION, APP_VERSION, now_str(),
                                    resumes=resumes_service.export_all())


def schema_description():
    """数据模型说明（/api/schema）。文案在 core/packaging.py，这里只把三个路径递进去。"""
    return packaging.schema_description(
        SCHEMA_VERSION,
        APP_VERSION,
        {"dir": DATA_DIR, "database": DB_PATH, "change_log": CHANGELOG_PATH},
    )

def import_package(payload):
    """导入标准化数据包。默认覆盖现有数据，改动日志保留。

    形状检查、逐条规整（跳没公司的、补默认阶段/结果、时间戳兜底）全在
    core/packaging.py —— 那边有测试，而且它测的是**默认覆盖模式**这种危险操作。
    这里只负责写库。
    """
    error = packaging.validate_package(payload)
    if error:
        return error
    stamp = now_str()
    entries = packaging.package_entries(payload, stamp)
    stage_rows = packaging.package_stages(payload)
    talk_rows = packaging.package_talks(payload, stamp)
    resume_rows = packaging.package_resumes(payload, stamp)
    mode = str(payload.get("mode") or "replace").lower()
    with store.db().locked_cursor() as cur:
        if mode == "replace":
            cur.execute("DELETE FROM events")
            cur.execute("DELETE FROM applications")
            cur.execute("DELETE FROM talks")
            # 简历（2026-10-10 加）：**只有数据包真的带了 resumes 才清**。
            # 老数据包没有这个键 → 保留现有简历；绝不能"因为包里没有就默认清空"。
            if "resumes" in payload:
                cur.execute("DELETE FROM resumes")
        imported = 0
        for entry in entries:
            cur.execute(
                """
                INSERT INTO applications
                    (company, category, position, applied_date, channel, link, notes, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    entry["company"],
                    entry["category"],
                    entry["position"],
                    entry["applied_date"],
                    entry["channel"],
                    entry["link"],
                    entry["notes"],
                    entry["created_at"],
                    entry["updated_at"],
                ),
            )
            app_id = cur.lastrowid
            for event in entry["events"]:
                cur.execute(
                    """
                    INSERT INTO events (application_id, stage, result, note, event_date, created_at, completed_at)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        app_id,
                        event["stage"],
                        event["result"],
                        event["note"],
                        event["event_date"],
                        event["created_at"],
                        event["completed_at"],
                    ),
                )
                cur.execute(
                    "INSERT OR IGNORE INTO stages(name, sort_order, is_preset) VALUES(?,?,0)",
                    (event["stage"], 500),
                )
            imported += 1
        for stage in stage_rows:
            cur.execute(
                "INSERT OR IGNORE INTO stages(name, sort_order, is_preset) VALUES(?,?,?)",
                (stage["name"], stage["sort_order"], stage["is_preset"]),
            )
        for talk in talk_rows:
            cur.execute(
                """
                INSERT INTO talks
                    (talk_date, company, time_slot, location, on_site, status,
                     rating, summary, questions, notes, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    talk["talk_date"],
                    talk["company"],
                    talk["time_slot"],
                    talk["location"],
                    talk["on_site"],
                    talk["status"],
                    talk["rating"],
                    talk["summary"],
                    talk["questions"],
                    talk["notes"],
                    talk["created_at"],
                    talk["updated_at"],
                ),
            )
        imported_resumes = resumes_service.import_rows(cur, resume_rows)
    # 事务边界与原来一致：先提交（出块时 store 提交），再写改动日志
    log_change(
        "import",
        "database",
        None,
        "导入数据包：%d 条投递（模式 %s）" % (imported, mode),
        before=None,
        after={"imported": imported, "mode": mode, "resumes": imported_resumes},
    )
    return None


def import_excel_package(payload):
    """按「导入模板.xlsx」一键导入。默认追加，不删除已有数据。

    列名映射 / 进展明细怎么拆 / 示例行怎么跳过，全在 core/excel.py（纯函数、有测试）；
    这里只负责「用 openpyxl 把表读成一行行」和写库。
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        # 把**后端正在用的解释器**一起报出来（2026-09-28 加）：这台机器上后端曾被挑到
        # 一个没装 openpyxl 的 Python（C:\Program Files\Python39），而依赖其实装在
        # 启动器自己用的那个里面 —— 只说"缺少 openpyxl"根本看不出是哪一份解释器的事。
        # launcher.py 的 find_python() 现在优先用"启动它的那个解释器"，这条错误是兜底。
        return ("缺少 openpyxl（后端解释器：%s）—— 关掉程序重新双击 启动.cmd 会自动装；"
                "也可以手动运行：\"%s\" -m pip install openpyxl"
                % (sys.executable, sys.executable))
    encoded = payload.get("data_base64") or ""
    if not encoded:
        return "没有收到 Excel 文件内容"
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return "文件内容解码失败"
    try:
        workbook = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    except Exception as err:
        return "无法读取 Excel 文件：%s" % err

    wanted = excel_rules.pick_sheet_name(workbook.sheetnames, excel_rules.SHEET_APPLICATIONS)
    sheet = workbook[wanted] if wanted else workbook.worksheets[0]

    rows = [[cell.value for cell in row] for row in sheet.rows]
    if not rows:
        return "表格是空的"
    cols = excel_rules.application_columns(excel_rules.header_of(rows[0]))
    if cols["company"] is None:
        return "模板里找不到「公司」这一列"

    mode = str(payload.get("mode") or "merge").lower()
    imported = 0
    skipped = 0
    with store.db().locked_cursor() as cur:
        if mode == "replace":
            cur.execute("DELETE FROM events")
            cur.execute("DELETE FROM applications")
        for row in rows[1:]:
            company = excel_rules.cell_text(row, cols["company"])
            if excel_rules.is_sample(company):
                skipped += 1
                continue
            position = excel_rules.cell_text(row, cols["position"])
            if cols["date"] is not None and cols["date"] < len(row):
                applied = _parse_excel_date(row[cols["date"]])
            else:
                applied = ""
            stamp = (applied + " 09:00:00") if applied else now_str()
            cur.execute(
                """
                INSERT INTO applications
                    (company, category, position, applied_date, channel, link, notes, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    company,
                    excel_rules.cell_text(row, cols["category"]),
                    position,
                    applied,
                    excel_rules.cell_text(row, cols["channel"]),
                    excel_rules.cell_text(row, cols["link"]),
                    excel_rules.cell_text(row, cols["notes"]),
                    stamp,
                    stamp,
                ),
            )
            app_id = cur.lastrowid
            # 这一行要生成哪些进展：有「进展明细」按明细拆，没有就用「当前阶段/当前结果」——
            # 规则都在 core/excel.py 的 row_events()，那边有测试。
            events = excel_rules.row_events(
                excel_rules.cell_text(row, cols["detail"]),
                excel_rules.cell_text(row, cols["stage"]),
                excel_rules.cell_text(row, cols["result"]),
                applied,
            )
            for stage, result, event_date in events:
                event_stamp = (event_date + " 09:00:00") if event_date else stamp
                completed_at = event_stamp if result == "已完成" else ""
                cur.execute(
                    """
                    INSERT INTO events (application_id, stage, result, note, event_date, created_at, completed_at)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (app_id, stage, result, "", event_date, event_stamp, completed_at),
                )
                cur.execute(
                    "INSERT OR IGNORE INTO stages(name, sort_order, is_preset) VALUES(?,?,0)",
                    (stage, 500),
                )
            imported += 1
        # 第二个工作表：宣讲会（可选，没有就跳过）
        imported_talks = 0
        talk_wanted = excel_rules.pick_sheet_name(workbook.sheetnames, excel_rules.SHEET_TALKS)
        if talk_wanted:
            talk_all = [[cell.value for cell in row] for row in workbook[talk_wanted].rows]
            if talk_all:
                tcols = excel_rules.talk_columns(excel_rules.header_of(talk_all[0]))
                if tcols["company"] is not None:
                    for row in talk_all[1:]:
                        talk_company = excel_rules.cell_text(row, tcols["company"])
                        if excel_rules.is_sample(talk_company):
                            continue
                        if tcols["date"] is not None and tcols["date"] < len(row):
                            talk_date = _parse_excel_date(row[tcols["date"]])
                        else:
                            talk_date = ""
                        talk_stamp = (talk_date + " 09:00:00") if talk_date else now_str()
                        cur.execute(
                            """
                            INSERT INTO talks
                                (talk_date, company, time_slot, location, on_site, status,
                                 rating, summary, questions, notes, created_at, updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                talk_date,
                                talk_company,
                                excel_rules.cell_text(row, tcols["time"]),
                                excel_rules.cell_text(row, tcols["location"]),
                                excel_rules.cell_text(row, tcols["on_site"]),
                                excel_rules.cell_text(row, tcols["status"]) or "待参加",
                                excel_rules.cell_text(row, tcols["rating"]),
                                excel_rules.cell_text(row, tcols["summary"]),
                                excel_rules.cell_text(row, tcols["questions"]),
                                excel_rules.cell_text(row, tcols["notes"]),
                                talk_stamp,
                                talk_stamp,
                            ),
                        )
                        imported_talks += 1
    # 事务边界与原来一致：先提交（出块时 store 提交），再写改动日志
    log_change(
        "import",
        "excel",
        None,
        "导入 Excel：%d 条投递、%d 场宣讲会（跳过 %d 行）"
        % (imported, imported_talks, skipped),
        before=None,
        after={"imported": imported, "talks": imported_talks, "skipped": skipped, "mode": mode},
    )
    return None


def _sync_log(stage, message):
    SYNC_STATE["stage"] = stage
    SYNC_STATE["message"] = message


def _fetch_list_items(url, attempts=2, timeout=50):
    """
    渲染列表页并解析，带重试。
    列表是 JS 异步渲染的，偶尔会拿到「还没渲染完」的空壳，
    所以解析不到条目时等一会儿再试一次。
    """
    last_html = None
    for attempt in range(attempts):
        last_html = render_page(url, timeout=timeout)
        if last_html:
            items = parse_talk_list(last_html)
            if items:
                return items, last_html
        if attempt + 1 < attempts:
            time.sleep(1.5)
    return [], last_html


def sync_gdut_talks(days_forward=None, fetch_details=True):
    """
    抓取「今天 + 后 N 天」的宣讲会并合并进 talks。
    N 默认取 config.json 的 "talk_sync_days"（30 天）——要跟学校网页上的列表对得上，
    窗口不能太窄（原来写死 2 天，网页上往后一个多月的场次永远抓不到）。
    合并原则：
      - 按 gdut_id 去重；
      - 已存在 -> 只更新日期/时间/地点/详情，口碑、状态、备注等你手填的内容不动；
      - 页面上已消失且日期未到 -> 标「已取消」；日期已过 -> 标「已过期」
        （只动「自动同步来的 + 状态还是待参加」的记录）。
    """
    if days_forward is None:
        days_forward = TALK_SYNC_DAYS
    today = datetime.now().date()
    start_date = today.strftime("%Y-%m-%d")
    end_date = (today + timedelta(days=days_forward)).strftime("%Y-%m-%d")
    report = {
        "ok": True, "date_range": [start_date, end_date], "found": 0,
        "added": 0, "updated": 0, "expired": 0, "cancelled": 0,
        "details": 0, "fit": {"strong": 0, "mid": 0, "low": 0}, "message": "",
    }
    _sync_log("list", "正在抓取 %s ~ %s 的宣讲会…" % (start_date, end_date))
    url = "%s?starttime=%s&endtime=%s" % (GDUT_LIST_URL, start_date, end_date)
    items, html = _fetch_list_items(url, attempts=2)
    if not items:
        # 兜底 1：换成和分页链接同一种「路径形式」的日期链接
        path_url = "%s/starttime/%s/endtime/%s/do123/career.gdut.edu.cn/domain/gdut" % (
            GDUT_LIST_URL, start_date, end_date
        )
        _sync_log("list", "第一次没抓到，换一种链接形式重试…")
        items, alt_html = _fetch_list_items(path_url, attempts=2)
        if items:
            html = alt_html
    if not items:
        # 兜底 2：抓默认列表页，再按日期本地过滤
        _sync_log("list", "再试一次默认列表页…")
        alt_items, alt_html = _fetch_list_items(GDUT_LIST_URL, attempts=2)
        if alt_items:
            filtered = [
                it for it in alt_items
                if start_date <= (it.get("talk_date") or "") <= end_date
            ]
            if filtered:
                items = filtered
                html = alt_html
    if not items:
        report["ok"] = False
        if not html:
            report["message"] = "无法打开宣讲会列表页，请检查网络，或确认已安装 Chrome / Edge"
        else:
            page_text = " ".join(html_to_lines(html)[:20])
            if ("ERR_NETWORK_ACCESS_DENIED" in html) or ("被禁止访问互联网" in page_text):
                # 浏览器被系统/防火墙禁止联网，这是最常见的一类失败
                report["message"] = (
                    "浏览器（Chrome/Edge）被系统禁止联网：ERR_NETWORK_ACCESS_DENIED。"
                    "常见原因有两个——"
                    "① 程序不是你自己双击启动的（例如被其他工具拉起），请关掉黑色窗口，"
                    "重新双击桌面的「应届海投管理」图标再试；"
                    "② 防火墙 / 杀毒软件拦截了 Chrome，请在防火墙里允许 Chrome 访问网络。"
                )
            else:
                snippet = " / ".join(html_to_lines(html)[:6])[:160]
                report["message"] = (
                    "列表页没有解析到数据（已自动重试并换过 3 种链接）。页面开头内容：%s" % snippet
                )
        _sync_log("error", report["message"])
        # 同步失败也写进改动日志，方便在「📜 记录」里回看失败原因
        log_change(
            "sync", "talk", None,
            "同步失败：%s" % report["message"],
            before=None, after=report,
        )
        return report
    # 列表是分页的（每页 20 条），把后面几页也抓下来
    page_urls = {1: url}
    for match in re.finditer(r'href="(/teachin/index/starttime/[^"]*?/page/(\d+))"', html):
        page_urls[int(match.group(2))] = "https://career.gdut.edu.cn" + match.group(1)
    max_page = min(max(page_urls.keys()) if page_urls else 1, 10)
    for page in range(2, max_page + 1):
        target_url = page_urls.get(page) or ""
        if not target_url:
            continue
        _sync_log("list", "正在抓取第 %d / %d 页…" % (page, max_page))
        extra_html = render_page(target_url)
        if extra_html:
            items += parse_talk_list(extra_html)
    # 按 gdut_id 去重（分页之间可能有重复）—— 判断在 core/talk_sync.py，那儿有测试
    items = dedupe_items(items)
    report["found"] = len(items)

    profile = load_profile()
    # 这一段只是**取快照**（读 existing / loose），读完就还连接；
    # 真正的合并写在下面第二个 locked_cursor 里。中间那段抓详情页是网络活，
    # 必须在锁外做（不然一次同步能把整个程序锁住几十秒）。
    with store.db().locked_cursor() as cur:
        existing = {
            row["gdut_id"]: dict(row)
            for row in cur.execute("SELECT * FROM talks WHERE gdut_id != ''")
        }
        # 还没绑定 gdut_id 的记录（你自己手填 / 从 DeepSeek 导入的那些），
        # 同步时按公司名合并进来，避免出现重复的两条。
        loose = [
            dict(row)
            for row in cur.execute("SELECT * FROM talks WHERE gdut_id = '' OR gdut_id IS NULL")
        ]

    if fetch_details:
        need = items_needing_detail(items, existing)
        if need:
            _sync_log(
                "detail",
                "正在抓取 %d 个详情页（并行 %d 个，约 %d 秒）…"
                % (len(need), DETAIL_WORKERS, max(6, len(need) * 5 // DETAIL_WORKERS)),
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
                results = list(
                    pool.map(lambda it: (it, render_page(GDUT_VIEW_URL % it["gdut_id"])), need)
                )
            for item, detail_html in results:
                if detail_html:
                    item["detail"] = parse_talk_detail(detail_html)
                    report["details"] += 1

    _sync_log("merge", "正在合并数据…")
    stamp = now_str()
    with store.db().locked_cursor() as cur:
        imported_ids = []
        for item in items:
            gdut_id = item.get("gdut_id") or ""
            if not gdut_id:
                continue
            imported_ids.append(gdut_id)
            detail = item.get("detail") or {}
            old = existing.get(gdut_id)
            if not old:
                # 还没绑定 gdut_id 的记录（你手填 / 从 DeepSeek 导入的）按公司名合并，
                # 免得同一家公司出现两条。匹配规则在 core/talk_sync.py，命中后自己从候选里拿掉。
                old = match_loose(loose, item)
                if old:
                    loose.remove(old)
            talk_type = detail.get("talk_type") or item.get("talk_type") or ""
            location = detail.get("location") or item.get("location") or ""
            if old:
                cur.execute(
                    """
                    UPDATE talks SET
                        gdut_id=?, source='gdut', company_std=?,
                        talk_date=?, location=?, time_slot=?, talk_type=?,
                        unit_nature=COALESCE(NULLIF(?, ''), unit_nature),
                        unit_industry=COALESCE(NULLIF(?, ''), unit_industry),
                        unit_scale=COALESCE(NULLIF(?, ''), unit_scale),
                        detail_text=COALESCE(NULLIF(?, ''), detail_text),
                        positions=COALESCE(NULLIF(?, ''), positions),
                        source_url=?, last_synced_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        gdut_id, item.get("company_std") or old.get("company_std") or "",
                        item.get("talk_date") or "", location, item.get("time_slot") or "", talk_type,
                        detail.get("unit_nature", ""), detail.get("unit_industry", ""),
                        detail.get("unit_scale", ""), detail.get("detail_text", ""),
                        detail.get("positions", ""), item.get("source_url") or "", stamp, stamp,
                        old["id"],
                    ),
                )
                report["updated"] += 1
            else:
                cur.execute(
                    """
                    INSERT INTO talks
                        (talk_date, company, company_std, time_slot, location, on_site,
                         status, rating, summary, questions, notes, gdut_id, source,
                         talk_type, unit_nature, unit_industry, unit_scale, detail_text,
                         positions, source_url, last_synced_at, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item.get("talk_date") or "", item.get("company") or "",
                        item.get("company_std") or "", item.get("time_slot") or "", location, "",
                        "待参加", "", "", "", "", gdut_id, "gdut", talk_type,
                        detail.get("unit_nature", ""), detail.get("unit_industry", ""),
                        detail.get("unit_scale", ""), detail.get("detail_text", ""),
                        detail.get("positions", ""), item.get("source_url") or "", stamp, stamp, stamp,
                    ),
                )
                report["added"] += 1

        # 同步之后要维护的状态（页面上撤了的 -> 已取消；日期过了还挂着的 -> 已过期）
        # 两条规则都在 core/talk_sync.py 的 plan_cleanup() 里，那儿有测试：
        # 只动 source='gdut' 且 status='待参加' 的记录，你手填的和改过状态的一律不碰。
        rows = [dict(r) for r in cur.execute("SELECT * FROM talks")]
        for talk_id, new_status in plan_cleanup(rows, imported_ids, start_date, end_date):
            cur.execute(
                "UPDATE talks SET status=?, updated_at=? WHERE id=?",
                (new_status, stamp, talk_id),
            )
            if new_status == "已过期":
                report["expired"] += 1
            else:
                report["cancelled"] += 1

        # 重新做对口初判（用户手动改过的跳过）
        cur.execute(
            "SELECT * FROM talks WHERE fit_manual=0 AND talk_date BETWEEN ? AND ?",
            (start_date, end_date),
        )
        for row in [dict(r) for r in cur.fetchall()]:
            level, reason = evaluate_fit(row, profile)
            cur.execute(
                "UPDATE talks SET fit_level=?, fit_reason=? WHERE id=?",
                (level, reason, row["id"]),
            )
            report["fit"][level] = report["fit"].get(level, 0) + 1

    report["merged"] = dedupe_talks()
    report["message"] = "新增 %d 场 · 更新 %d 场 · 合并重复 %d 条 · 逾期 %d 场 · 取消 %d 场 · 详情 %d 个" % (
        report["added"], report["updated"], report["merged"],
        report["expired"], report["cancelled"], report["details"]
    )
    log_change(
        "sync", "talk", None,
        "同步宣讲会：%s（%s ~ %s）" % (report["message"], start_date, end_date),
        before=None, after=report,
    )
    with store.db().locked_cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('last_talk_sync', ?)", (stamp,)
        )
    SYNC_STATE["last_sync"] = stamp
    _sync_log("done", report["message"])
    return report


def start_sync_async(fetch_details=True):
    """后台跑同步，避免卡住页面。同一时间只会跑一个。"""
    if SYNC_STATE.get("running"):
        return False

    def worker():
        SYNC_STATE["running"] = True
        add_busy(1)   # 同步期间窗口关了也不退出，别把活儿掐一半
        try:
            SYNC_STATE["report"] = sync_gdut_talks(fetch_details=fetch_details)
        except Exception as err:
            SYNC_STATE["report"] = {"ok": False, "message": "同步出错：%s" % err}
            _sync_log("error", "同步出错：%s" % err)
            log_change("sync", "talk", None, "同步异常：%s" % err, before=None, after=None)
        finally:
            SYNC_STATE["running"] = False
            add_busy(-1)

    threading.Thread(target=worker, daemon=True).start()
    return True


def dedupe_talks():
    """
    合并重复记录：把「没有 gdut_id」的旧记录（你自己填的 / 从 DeepSeek 导入的）
    合并进「有 gdut_id」的同步记录，避免同一家公司出现两条。
    """
    merged = 0
    merge_audit = []
    with store.db().locked_cursor() as cur:
        synced = [dict(r) for r in cur.execute("SELECT * FROM talks WHERE gdut_id != ''")]
        loose = [
            dict(r)
            for r in cur.execute("SELECT * FROM talks WHERE gdut_id = '' OR gdut_id IS NULL")
        ]
        for candidate in loose:
            key = company_key(candidate.get("company_std") or candidate.get("company") or "")
            if not key:
                continue
            target = None
            for row in synced:
                row_key = company_key(row.get("company_std") or row.get("company") or "")
                if row_key and row_key == key:
                    target = row
                    break
            if target is None:
                for row in synced:
                    row_key = company_key(row.get("company_std") or row.get("company") or "")
                    if len(row_key) >= 3 and (row_key in key or key in row_key):
                        target = row
                        break
            if not target:
                continue
            cur.execute(
                """
                UPDATE talks SET
                    rating = CASE WHEN rating = '' THEN ? ELSE rating END,
                    summary = CASE WHEN summary = '' THEN ? ELSE summary END,
                    questions = CASE WHEN questions = '' THEN ? ELSE questions END,
                    notes = CASE WHEN notes = '' THEN ? ELSE notes END,
                    research_industry = CASE WHEN research_industry = '' THEN ? ELSE research_industry END,
                    research_sources = CASE WHEN research_sources = '' THEN ? ELSE research_sources END,
                    research_at = CASE WHEN research_at = '' THEN ? ELSE research_at END,
                    on_site = CASE WHEN on_site = '' THEN ? ELSE on_site END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    candidate.get("rating") or "", candidate.get("summary") or "",
                    candidate.get("questions") or "", candidate.get("notes") or "",
                    candidate.get("research_industry") or "", candidate.get("research_sources") or "",
                    candidate.get("research_at") or "", candidate.get("on_site") or "",
                    now_str(), target["id"],
                ),
            )
            cur.execute("DELETE FROM talks WHERE id=?", (candidate["id"],))
            merge_audit.append((candidate, target["id"], target.get("company")))
            merged += 1
    # 事务边界与原来一致：先提交（出块时 store 提交），再把审计补上
    for candidate, target_id, target_company in merge_audit:
        log_change(
            "merge", "talk", target_id,
            "合并重复宣讲会：%s（旧记录 %s 已并入）"
            % (target_company, candidate.get("company")),
            before=candidate, after={"into": target_id},
        )
    return merged


def refresh_fit_all():
    """按当前个人画像，对所有宣讲会重算对口初判（手动改过的跳过）。"""
    profile = load_profile()
    with store.db().locked_cursor() as cur:
        rows = [dict(r) for r in cur.execute("SELECT * FROM talks WHERE fit_manual=0")]
        for row in rows:
            # 老数据可能还没有标准公司名，这里补上（同步时靠它做合并匹配）
            if not (row.get("company_std") or "").strip():
                standard = normalize_company(row.get("company") or "")
                row["company_std"] = standard
                cur.execute(
                    "UPDATE talks SET company_std=? WHERE id=?", (standard, row["id"])
                )
            level, reason = evaluate_fit(row, profile)
            cur.execute(
                "UPDATE talks SET fit_level=?, fit_reason=? WHERE id=?",
                (level, reason, row["id"]),
            )


def maybe_auto_sync():
    """启动时调用：距上次同步超过 AUTO_SYNC_HOURS 小时才自动同步。"""
    last = ""
    try:
        with store.db().locked_cursor() as cur:
            row = cur.execute("SELECT value FROM meta WHERE key='last_talk_sync'").fetchone()
        last = row["value"] if row else ""
    except Exception:
        last = ""
    if last:
        try:
            gap = (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
            if gap < AUTO_SYNC_HOURS * 3600:
                SYNC_STATE["last_sync"] = last
                return False
        except ValueError:
            pass
    return start_sync_async()


# 组装「口碑 / 风险摘要」时用到的字段（顺序即展示顺序：重点在前）
# 2026-09-24：调研的常量搬到 services/research.py（S4）。
RESEARCH_SUMMARY_FIELDS = research_service.RESEARCH_SUMMARY_FIELDS

# 2026-09-24：调研的常量搬到 services/research.py（S4）。
RESEARCH_PROMPT_LINES = research_service.RESEARCH_PROMPT_LINES

# 2026-09-24：调研的常量搬到 services/research.py（S4）。
RESEARCH_PROMPT = research_service.RESEARCH_PROMPT

def build_research_task(limit=None, start=None, end=None):
    """
    实现已搬到 `services/research.py`（2026-09-24，S4）。这里只是转发。

    生成「调研任务包」文本：待调研公司清单 + 严格提示词。
    一次最多列 RESEARCH_BATCH_SIZE 家——一次给 AI 太多公司，质量会明显下降。
    导入这一批之后，再点一次会自动给出下一批。

    范围：**今天及以后、还没调研过的宣讲会**。
    —— 早先这里只扫「近三天」，结果今天 19 号、22 号的场次明明已经同步进列表了，
       却因为超出窗口而进不了任务包，只能干等两天。学校的**同步**窗口是近三天，
       但**调研**没必要卡这个限制：只要还没过期就该能提前调研。
    已取消 / 已过期的场次不列（反正也用不上了）。

    start / end 一般不用传（默认「今天起、不设上限」），留出参数是为了方便测试时
    指定别的日期区间。

    返回 `(任务包文本, 错误信息)`（2026-09-25 改，原来只返回文本）：
    个人画像没填主专业时返回 `(None, "请先填画像…")`。
    为什么改签名：提示词里的「对口判断」标准要按**用户本人的专业**生成，
    画像空着的话 AI 只会瞎猜，给出来的结论全是废的 —— 与其给一份会误导人的任务包，
    不如明确让用户先去填。
    """
    return research_service.build_research_task(limit, start, end)



# 2026-09-24：调研的常量搬到 services/research.py（S4）。
SUMMARY_LABEL_RE = research_service.SUMMARY_LABEL_RE


def _split_summary(text):
    """
    实现已搬到 `services/research.py`（2026-09-24，S4）。这里只是转发。

    把已有摘要按【字段】拆成 [(字段, 内容), ...]。
    局部补充（例如只粘贴小红书那一条）时用它做字段级合并，避免把已调研的内容冲掉。
    """
    return research_service._split_summary(text)



def apply_research(text):
    """
    实现已搬到 `services/research.py`（2026-09-24，S4）。这里只是转发。

    把粘贴回来的调研结果写进对应的宣讲会记录（支持完整调研与局部补充）。
    """
    return research_service.apply_research(text)



# --------------------------------------------------------------------------- #
# HTTP 服务：所有接口都在 Handler 里
# 约定：
#   - 读接口返回 build_state() 或专门的数据；
#   - 写接口处理完后统一返回 build_state()，前端会用新 state 整体刷新；
#   - 出错时返回 {"error": "..."}，前端会弹提示。
#   - 路由集中在 do_GET / do_POST / do_PUT / do_DELETE，新增接口记得两边同步
#     （web/index.html 的 localApi 里也有一份分享版的镜像）。
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# HTTP 路由按域拆到 api/ 包里（2026-09-22）
#   下面那行 sys.modules.setdefault 是必须的：api/*.py 顶部写的是 from server import ...，
#   而本文件直接跑起来时模块名是 "__main__"、不在 sys.modules 里 —— 不登记的话，
#   api 模块会把本文件**再执行一遍**（会出现两份模块状态：两份 _ALIVE、两套初始化流程）。
#   放在这里（所有函数都定义完之后、class Handler 之前）才 import 得到东西。
# --------------------------------------------------------------------------- #
sys.modules.setdefault("server", sys.modules[__name__])
from api.apps import AppsRoutes                # noqa: E402
from api.core import CoreRoutes                # noqa: E402
from api.desktop import DesktopRoutes          # noqa: E402
from api.io import IoRoutes                    # noqa: E402
from api.notes import NotesRoutes              # noqa: E402
from api.resumes import ResumesRoutes          # noqa: E402
from api.static_files import StaticRoutes      # noqa: E402
from api.talks import TalksRoutes              # noqa: E402
from api.update import UpdateRoutes            # noqa: E402

# 每个 HTTP 方法的域链。顺序 = 拆分前各域分支**首次出现**的顺序；
# 域之间、域内的条件都是互斥的（精确 path 匹配 / 互不重叠的正则），所以顺序不影响行为。
# 要看完整的路由清单：python tools	est_routes.py
GET_CHAIN = ("get_static", "get_core", "get_desktop", "get_talks", "get_io", "get_resumes", "get_update")
POST_CHAIN = ("post_apps", "post_notes", "post_talks", "post_io", "post_resumes", "post_update")
PUT_CHAIN = ("put_apps", "put_talks", "put_notes", "put_resumes")
DELETE_CHAIN = ("delete_apps", "delete_talks", "delete_notes", "delete_resumes")


class Handler(StaticRoutes, CoreRoutes, DesktopRoutes, AppsRoutes, NotesRoutes, ResumesRoutes,
              TalksRoutes, IoRoutes, UpdateRoutes, BaseHTTPRequestHandler):
    server_version = "ResumeTracker/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 控制台保持干净
        pass

    # ---------- 输出工具 ---------- #
    def _send_bytes(self, body, content_type, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _serve_file(self, path, content_type):
        if not os.path.isfile(path):
            self._send_json({"error": "文件不存在"}, 404)
            return
        with open(path, "rb") as handle:
            body = handle.read()
        self._send_bytes(body, content_type)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else {}

    # ---------- GET ---------- #

    def _run_chain(self, chain, path, *args):
        """
        跑一条域链，**把意外异常记下来**（2026-09-25 加）。

        为什么要有这一层：原来四个 `do_*` 里任何一条域链抛异常，
        表现都是「页面转圈 / 弹一句连不上」，而**服务端的 traceback 被丢掉了**
        （`launcher.py` 起服务时 stdout/stderr 都指向 DEVNULL）。
        用户在别的电脑上遇到这个，我们手里一条线索都没有。

        现在：异常写进 `data/error.log`（打开就是问题本身）+ `logs/server.log`（stderr），
        并且**如实回一个 500**，让前端能看到"是后台出错了"而不是"连不上"。
        注意：HTTP 头一旦发出去就不能再改，所以只在还没响应时补 500。
        """
        try:
            for name in chain:
                if getattr(self, name)(path, *args):
                    return
            self._send_json({"error": "接口不存在"}, 404)
        except Exception as exc:
            diagnostics_service.log_exception("接口 %s %s" % (self.command, path), exc)
            try:
                self._send_json({"error": "后台出错，已记进 data/error.log"}, 500)
            except Exception:
                pass

    def do_GET(self):
        path = urlparse(self.path).path
        self._run_chain(GET_CHAIN, path)


    # ---------- POST ---------- #
    def do_POST(self):
        path = urlparse(self.path).path
        payload = self._read_json()
        # 桌面模式的三个接口先看（心跳 / 关闭信号 / 退出）——它们的 body 是空的，
        # 所以必须排在「payload 为空就报 400」前面（顺序与拆分前的 do_POST 一致）。
        if self.post_desktop(path, payload):
            return
        if payload is None:
            self._send_json({"error": "请求数据格式错误"}, 400)
            return
        self._run_chain(POST_CHAIN, path, payload)


    # ---------- PUT ---------- #
    def do_PUT(self):
        path = urlparse(self.path).path
        payload = self._read_json()
        if payload is None:
            self._send_json({"error": "请求数据格式错误"}, 400)
            return
        self._run_chain(PUT_CHAIN, path, payload)


    # ---------- DELETE ---------- #
    def do_DELETE(self):
        path = urlparse(self.path).path
        self._run_chain(DELETE_CHAIN, path)


    # ---------- 导出 ---------- #
    def _export_json(self):
        body = json.dumps(export_package(), ensure_ascii=False, indent=2).encode("utf-8")
        filename = "简历投递备份_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
        self._send_bytes(
            body,
            "application/json; charset=utf-8",
            200,
            {"Content-Disposition": "attachment; filename*=UTF-8''" + _quote(filename)},
        )

    def _export_csv(self):
        state = build_state()
        buffer = io.StringIO()
        buffer.write("\ufeff")
        writer = csv.writer(buffer)
        writer.writerow(packaging.csv_header())
        # 行怎么拼（含「完整进展」那一列的时间线格式）在 core/packaging.py，那边有测试
        for row in packaging.csv_rows(state):
            writer.writerow(row)
        body = buffer.getvalue().encode("utf-8")
        filename = "简历投递清单_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
        self._send_bytes(
            body,
            "text/csv; charset=utf-8",
            200,
            {"Content-Disposition": "attachment; filename*=UTF-8''" + _quote(filename)},
        )


def _quote(text):
    from urllib.parse import quote

    return quote(text)


def find_free_port(start):
    for port in range(start, start + 30):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((HOST, port))
            probe.close()
            return port
        except OSError:
            probe.close()
    return start


class LocalHTTPServer(ThreadingHTTPServer):
    """
    本地 HTTP 服务：把「客户端自己断开」降成一行日志（2026-09-27 加）。

    为什么单独立一个类：页面刷新、切视图、关窗口都会**取消正在飞的请求**，
    这时 `rfile.read()` 会吃 `ConnectionResetError`（Windows 上是 WinError 10054）。
    `socketserver` 默认把它当"未处理异常"，往 stderr 打**一整段 traceback**；
    而 1.1 起 stderr 接到了 `logs/server.log` —— 真机上实测积了十几条，
    诊断报告的「日志末尾」全是这种假崩溃，真错误反而被埋掉。

    所以这里只对**客户端主动断开**这三类降级成一行；其它异常一律照旧
    走父类（打印完整 traceback），该有的现场一个都不少。
    """

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            try:
                where = client_address[0] if client_address else "?"
                sys.stderr.write("客户端提前断开（%s）：%s\n" % (where, type(exc).__name__))
                sys.stderr.flush()
            except Exception:
                pass
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def main():
    """
    启动入口：
      0. 先备份一份现有数据（backup.py，失败也不影响启动）；
      1. init_db() 建库 / 迁移；
      2. 从 DEFAULT_PORT（可被 config.json 覆盖）开始找一个空闲端口；
      3. 启动本地 HTTP 服务；
      4. 普通模式自动打开浏览器；桌面模式把端口写给 launcher.py，由它开窗口。

    桌面模式（config.json 里 "desktop": true）额外做三件事：
      - 写 data/runtime.json，告诉启动器实际端口；
      - 启动看门狗线程，窗口全关掉后自动退出；
      - 退出时把 runtime.json 删掉，避免启动器读到过期端口。
    """
    # 先拒绝半新半旧程序；在备份、迁移和任何业务写入之前检查，只让独立更新器恢复。
    _txn_ok, _txn_reason = update_txn.check_startup(BASE_DIR)
    print("  升级事务：%s" % _txn_reason)
    if not _txn_ok:
        app_paths.record_startup_error(_txn_reason)
        return 2
    # 先把库路径装进 store（幂等）。放最前面是让「store 已就绪」成为启动后的既定事实，
    # 而不是要等到第一次用数据库时才隐式发生。
    _ensure_store()
    # 声明 DPI 感知（2026-09-27 加）：这个进程没有窗口，但**诊断报告里的屏幕尺寸/缩放
    # 是它读的** —— 不感知的进程只会读到一个被 Windows 虚拟化过的值（缩放恒为 96），
    # 而"缩放 != 100% 的机器"恰恰是这个程序最容易出窗口问题的地方（见 winenv.py 顶部）。
    log_line = winenv.enable_dpi_awareness()
    print("  解释器：Python %s（%d 位）" % (sys.version.split()[0], ctypes.sizeof(ctypes.c_void_p) * 8))
    print("  DPI 感知：%s（显示器 %d 台，%d DPI）"
          % (log_line, winenv.display_count(), winenv.system_dpi()))
    # 先说清楚这次用的是哪套目录（出问题时第一眼要看的就是它：数据到底在程序隔壁还是用户目录）
    print("  路径：%s" % app_paths.describe())
    app_paths.clear_startup_error()      # 走到这儿说明启动没死在"路径/数据目录"上，清掉上一次的失败记录
    # 排查现场（2026-09-25 加）：把日志/数据目录注进去，并装上后台线程的异常钩子。
    # ⚠️ 必须在这里（init_db 之前）装 —— 后面 init_db / 自动同步都在后台线程里跑，
    # 装晚了一步它们崩了就还是没有记录。
    diagnostics_service.use(LOG_DIR, DATA_DIR)
    diagnostics_service.install_thread_hook()
    # 先备份、再动数据库：init_db() 里会给老库补字段（ALTER TABLE），
    # 万一哪天改坏，手里至少有升级前那一刻的副本。备份失败只是没有备份，不拦启动。
    # 2026-10-11：data/profile 的路径显式传进去（base_dir 只用来算"要不要备份"的边界，不再由它推 data）。
    state_service.set_backup(backup.run_startup_backup(
        BASE_DIR, BACKUP_LOG_PATH, data_dir=DATA_DIR, profile_path=PROFILE_PATH))
    # 启动器会把外壳控制端口传进来（--shell-port N）。拿到了就只认那个端口的关闭信号。
    # ⚠️ 必须走 `set_shell_port()`：真正读这个值的是 services/lifecycle.touch_closing，
    # 直接写 `SHELL_PORT = N` 只改 server 这一份别名，校验会失效（来源校验静默失灵）。
    if "--shell-port" in sys.argv[1:]:
        try:
            _lifecycle.set_shell_port(int(sys.argv[sys.argv.index("--shell-port") + 1]))
        except (IndexError, ValueError):
            _lifecycle.set_shell_port(0)
    else:
        _lifecycle.set_shell_port(0)
    # ★ 能不能打开这个库？**必须在任何迁移/业务写入之前判**（方案 §3.5.1 第四节）。
    #   判的是**库里真实记录的版本**（store.read_schema_version），不是程序常量 ——
    #   理由与 S/R/M 三张表见 services/schema_compat.py 顶部。
    #   拒绝时：写启动失败通道（启动器能弹出来）+ 不碰数据库一个字，然后退出。
    _ensure_store()
    try:
        _compat_ok, _compat_reason = schema_compat.check(store.read_schema_version())
    except Exception:
        _compat_ok, _compat_reason = False, "无法可靠读取数据库结构版本，请检查数据库是否损坏或被占用。"
    print("  数据库兼容：%s" % _compat_reason)
    if not _compat_ok:
        app_paths.record_startup_error("拒绝打开数据库：%s" % _compat_reason)
        print("  [X] %s" % _compat_reason)
        return 2
    # ★ 内置更新器（A 档：后台一天一次 + 用户点确认才装）。只读状态文件、不碰数据库；
    #   真下载/安装由用户点了才做（编排见 services/update_service.py）。
    #   更新器路径：便携包在 <包>\updater\updater.py（**在程序目录之外**），开发仓库在 <仓>/tools/updater.py。
    _updater = next((p for p in (os.path.join(os.path.dirname(BASE_DIR), "updater", "updater.py"),
                                 os.path.join(os.path.dirname(BASE_DIR), "tools", "updater.py"))
                     if os.path.isfile(p)), None)
    update_service.configure(APP_VERSION, BASE_DIR, updater_path=_updater)
    update_service.maybe_auto_check()
    init_db()
    load_profile()        # 首次运行会生成「个人画像.json」，之后以文件为准
    refresh_fit_all()     # 按当前画像重算一遍对口初判
    backfill_talk_applications()  # 已投递但还没同步到投递列表的场次，补一次
    maybe_auto_sync()     # 距上次同步超过 AUTO_SYNC_HOURS 小时才后台自动同步
    port = find_free_port(DEFAULT_PORT)
    url = "http://%s:%d/" % (HOST, port)
    server = LocalHTTPServer((HOST, port), Handler)
    # 让 /api/shutdown 能真正把服务停掉（serve_forever 会返回）
    _SHUTDOWN["fn"] = server.shutdown
    _ALIVE["beat"] = time.time()
    if DESKTOP_MODE:
        write_runtime_file(port)
        threading.Thread(target=desktop_watchdog, daemon=True).start()
        print("  桌面模式：窗口由启动器打开（%s）" % url)
        print("  %s %s（%s）" % (edition_label(), APP_VERSION, DB_PATH))
    else:
        print("=" * 56)
        print("  应届海投管理 已启动")
        print("  版本： %s %s" % (edition_label(), APP_VERSION))
        print("  打开地址： %s" % url)
        print("  数据文件： %s" % DB_PATH)
        print("  关闭这个窗口即可停止程序（数据不会丢失）")
        print("=" * 56)
        if "--no-browser" not in sys.argv:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
        remove_runtime_file()


# --------------------------------------------------------------------------- #
# 宣讲会同步：广东工业大学就业网
# 说明：列表页和详情页都是 JS 异步加载，直接抓 HTML 拿不到数据，
#       必须用无界面浏览器渲染后再解析（见 render_page）。
# --------------------------------------------------------------------------- #
def find_browser():
    for path in BROWSER_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def render_page(url, timeout=50):
    """
    用无界面浏览器渲染页面并返回 HTML；失败返回 None。

    注意：广工就业网的列表是页面加载后才由 JS 渲染出来的，
    所以必须等页面渲染完（--virtual-time-budget），
    而且偶尔会因为网络慢而拿到「还没渲染出内容」的空壳，调用方需要重试。
    """
    exe = find_browser()
    if not exe:
        return None
    profile_dir = tempfile.mkdtemp(prefix="gdut_render_")
    args = [
        exe, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
        "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run", "--log-level=3",
        "--virtual-time-budget=30000", "--user-data-dir=" + profile_dir,
        "--dump-dom", url,
    ]
    try:
        result = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout
        )
        return result.stdout.decode("utf-8", "replace")
    except Exception:
        return None
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def open_logs_folder():
    """
    在资源管理器里打开日志文件夹（2026-09-25 加）。

    给「出问题了，想把原始日志发出去」的用户用。**只开我们自己的 logs 目录**，
    不接受任何参数 —— 不能变成"用接口打开任意路径"的口子。

    为什么用 Popen 而不是 subprocess.run：`explorer` 会立刻返回，但等它退出没必要；
    而且 `CREATE_NO_WINDOW` 是必须的 —— 否则会闪一个黑框（这个项目为"黑窗口"
    已经踩过好几次了，见 `AGENTS.md`）。

    ⚠️ **自检环境不许真开窗口**（2026-10-19 加）：接口门/路由门会真的调这个接口，
    于是每次跑自检都在桌面上弹出一个「临时副本的 logs 目录」，而且没人关它
    （用户实测：攒一堆）。约定：**只要设了 `RESUME_TRACKER_NO_SHELL=1`**（自检脚本会设），
    就只做目录/轮转检查、不调 `explorer`，返回 True（接口行为不变，只是不弹窗）。
    """
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR)
        if app_paths.rotation_enabled():
            app_paths.rotate_log(os.path.join(LOG_DIR, "server.log"))
        if os.environ.get("RESUME_TRACKER_NO_SHELL") == "1":
            diagnostics_service.log_exception("自检环境：跳过打开资源管理器（RESUME_TRACKER_NO_SHELL=1）")
            return True
        flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(["explorer", LOG_DIR], creationflags=flags)
        return True
    except Exception:
        diagnostics_service.log_exception("打开日志文件夹失败")
        return False


def load_profile():
    """
    读取个人画像；没有就按默认生成一份，方便用户自己改。实现见 `services/profile.py`。

    ⚠️ 必须先 `_ensure_store()` 把画像路径注进去 —— 否则 services 那层拿到的是空路径，
    读不到也写不出（写失败会被那边的 try/except 静默吞掉，表现为「文件没生成」）。
    这正是 2026-09-24 搬迁时探针抓到的一处漏注入。
    """
    _ensure_store()
    return profile_service.load_profile()



if __name__ == "__main__":
    sys.exit(main())
