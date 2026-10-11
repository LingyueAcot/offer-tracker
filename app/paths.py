# -*- coding: utf-8 -*-
r"""paths.py —— **程序文件在哪、用户数据/日志/缓存在哪**，全项目唯一一处决定路径的地方。

【为什么要单独一个模块】在它之前，路径是散在 `launcher.py` / `server.py` / `services/diagnostics.py`
里各写一遍的（`BASE_DIR + "data"` 之类十几处）。那样有两个后果：
  ① 想把"新装的数据放 `%LOCALAPPDATA%\ResumeTracker\`"这件事做成，就得改十几处、还要保证它们一致；
  ② 自检脚本复制一份程序起来跑时，**没有任何办法**让它别写进用户真实的那份目录。
现在所有路径都从 `app/paths.py` 出，自检只要设一个环境变量就能把整套 I/O 圈进临时目录。
**新增路径请加在这里，不要再去拼 `BASE_DIR`。**

【四种装法怎么认】（按优先级从上往下，第一个成立的说了算）

| # | 判据 | mode | 数据放哪 | 谁在用 |
|---|---|---|---|---|
| ① | 环境变量 `RESUME_TRACKER_DATA_DIR` | `env` | 就用它 | **自检/隔离副本**（最高优先，专为"别碰用户数据"） |
| ② | `config.json` 里写了 `data_dir` | `config` | 用它（相对路径相对程序目录） | **便携包**（包里写 `"data_dir": "data"`，明确声明"数据在我自己旁边"）/ 高级用户 |
| ③ | 程序目录旁的 `data\` **这棵目录**存在 | `inplace` | `<程序>\data` | **老装（含用户这台电脑这份）** |
| ④ | 上面都不成立 | `user` | `%LOCALAPPDATA%\ResumeTracker\` | **新装** |

③ 的判据是"`<程序>\data\` 这棵目录在"，**不是**"里面有没有 `applications.db`"。
为什么：只看库文件的话，用户一旦误删库（日志/备份还在），程序会以为自己是"新装"、
跑去用户目录**建一个空库** —— 他会看到"我的数据没了"。目录还在就说明这套装法一直是原地模式，
库该在那个目录里重建，而不是换个地方另起一份（2026-10-11 外部评审 #2 点出来的）。

【布局：老装与便携保持"贴着程序"，只有新装用嵌套】
    · 数据目录 == `<程序>\data`（原地 / 便携 / 相对路径配置）
        → 日志 `<程序>\logs`、浏览器 profile `<程序>\browser_profile`、`<程序>\webview_profile`
    · 数据目录在别处（新装 / 绝对路径配置）
        → 日志 `<数据目录>\logs`、profile `<数据目录>\cache\...`
**为什么这么分**：老装的历史布局就是贴着程序的，把日志/profile 跟着 `data_dir` 搬走会**改变老装的实际路径**
（WebView2 profile 一挪，主题等 localStorage 就"丢了"；`data\` 的体积也会被浏览器缓存撑大）。
所以：**只有"数据目录不是 `<程序>\data`"时才用嵌套布局**（同样来自 外部评审 #11，我第一版写错了）。

【绝不静默换目录】要写数据时目录不可用就**报错**，不许悄悄换个地方建个空库。

【自检】
    python app\paths.py --selftest      # 用假输入验判定顺序与布局（不碰真实目录）
"""
from __future__ import print_function

import json
import os
import sys

APP_NAME = "ResumeTracker"          # 用户目录下的文件夹名（历史沿用，别改：老快捷方式认它）
ENV_DATA_DIR = "RESUME_TRACKER_DATA_DIR"   # 与项目既有约定一致（RESUME_TRACKER_MUTEX / _DESKTOP）
DB_NAME = "applications.db"
CHANGELOG_NAME = "change_log.jsonl"
PROFILE_NAME = "个人画像.json"
EXCEL_TEMPLATE_NAME = "导入模板.xlsx"
AI_PROMPT_NAME = "AI转换提示词.md"
CONFIG_NAME = "config.json"
# 判"隔壁这棵 data 目录是不是这套装法的数据家"用的证据（有任意一个就够了）
INPLACE_TRACES = (DB_NAME, CHANGELOG_NAME, "backups", "logs", "window_state.json", "runtime.json")

_resolved = {}


def program_dir():
    """程序目录（`app\\`）：launcher.py / server.py / web\\ 都在这儿。"""
    return os.path.dirname(os.path.abspath(__file__))


def _local_appdata():
    value = os.environ.get("LOCALAPPDATA")
    if value:
        return value
    return os.path.join(os.path.expanduser("~"), "AppData", "Local")


def _config_data_dir(config_path):
    """从 config.json 里读可选的 `data_dir`（纯读，不碰 globals）。

    区分三种情况（2026-10-11 收紧，外部评审 #3）：
      · None  —— 文件不存在（正常），或没有这个键
      · "..." —— 有且是合法的非空字符串
      文件存在却**读不动 / 不是合法 JSON / 顶层不是对象 / data_dir 不是非空字符串** → 抛 ValueError。
    为什么不再"静默当作没写"：配错路径的人最需要看到"你的配置坏了"；
    静默忽略会让他以为程序在用配置里那个目录，实际用的是别处。
    """
    if not os.path.isfile(config_path):
        return None
    with open(config_path, "rb") as handle:
        raw = handle.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as err:
        raise ValueError("配置文件不是 UTF-8（%s）：%s" % (config_path, err))
    try:
        data = json.loads(text)
    except ValueError as err:
        raise ValueError("配置文件不是合法 JSON（%s）：%s" % (config_path, err))
    if not isinstance(data, dict):
        raise ValueError("配置文件的顶层不是对象（%s）" % config_path)
    value = data.get("data_dir")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("config.json 的 data_dir 必须是非空字符串，实际是 %r" % (value,))
    return value.strip()


def _has_inplace_traces(program):
    """程序旁边的 `data\\` 是不是"这套装法的数据家"（见模块开头 ③ 的说明）。"""
    data = os.path.join(program, "data")
    if not os.path.isdir(data):
        return False
    if os.path.isfile(os.path.join(data, DB_NAME)):
        return True
    for name in INPLACE_TRACES:
        if os.path.exists(os.path.join(data, name)):
            return True
    try:
        return not os.listdir(data)          # 目录在但空着（用户手动清过）——仍认原地
    except OSError:
        return True


def resolve(program, env, config_value, local_appdata, has_traces=None):
    """**纯函数**：判定顺序都在这儿，便于自检用假输入验（不碰真实目录）。

    返回 `(data_dir, mode)`；mode ∈ {"env", "config", "inplace", "user"}。
    `has_traces` 只为自检方便（不给就按真实文件系统判断）。
    """
    env_value = (env or {}).get(ENV_DATA_DIR)
    if env_value and env_value.strip():
        return os.path.abspath(env_value.strip()), "env"
    if config_value and config_value.strip():
        value = config_value.strip()
        if not os.path.isabs(value):
            value = os.path.join(program, value)     # 便携包写 "data" ⇒ <程序>\data
        return os.path.abspath(value), "config"
    if has_traces is None:
        has_traces = _has_inplace_traces(program)
    if has_traces:
        return os.path.join(program, "data"), "inplace"
    return os.path.join(local_appdata, APP_NAME), "user"


def data_dir():
    """数据目录（库、审计日志、备份、窗口位置都在里面）。"""
    if "data" not in _resolved:
        program = program_dir()
        try:
            config_value = _config_data_dir(os.path.join(program, CONFIG_NAME))
        except ValueError as err:
            # 配置坏了就明说（并让程序起不来）：随便挑个目录更糟。见 _config_data_dir 的说明。
            raise RuntimeError(str(err))
        value, mode_value = resolve(program, os.environ, config_value, _local_appdata())
        _resolved["data"] = value
        _resolved["mode"] = mode_value
    return _resolved["data"]


def mode():
    """当前是哪种装法：env / config / inplace / user（见模块开头那张表）。"""
    data_dir()
    return _resolved["mode"]


def layout_adjacent(program, data):
    """纯函数：数据目录就是"程序隔壁那个 data"吗？（决定日志/profile 用哪套布局）"""
    return os.path.normcase(os.path.abspath(data)) == \
        os.path.normcase(os.path.abspath(os.path.join(program, "data")))


def _adjacent():
    return layout_adjacent(program_dir(), data_dir())


def logs_dir():
    """日志目录。老装/便携：`<程序>\\logs`（历史位置，一个字都不许变）；
    只有"数据目录不在程序隔壁"（新装 / 自定义绝对路径）时才用 `<数据目录>\\logs`。"""
    if _adjacent():
        return os.path.join(program_dir(), "logs")
    return os.path.join(data_dir(), "logs")


def cache_dir():
    """缓存目录（浏览器 profile 等几十上百 MB 的东西，**故意不放进 data\\**）。"""
    if _adjacent():
        return program_dir()
    return os.path.join(data_dir(), "cache")


def config_path():
    """配置文件。**永远在程序目录**（它描述"怎么启动"，不属于用户数据）。"""
    return os.path.join(program_dir(), CONFIG_NAME)


def profile_path():
    return os.path.join(program_dir(), PROFILE_NAME)


def excel_template():
    return os.path.join(program_dir(), EXCEL_TEMPLATE_NAME)


def ai_prompt():
    return os.path.join(program_dir(), AI_PROMPT_NAME)


def web_dir():
    return os.path.join(program_dir(), "web")


def db_path():
    return os.path.join(data_dir(), DB_NAME)


def changelog_path():
    return os.path.join(data_dir(), CHANGELOG_NAME)


def backup_dir():
    return os.path.join(data_dir(), "backups")


def runtime_path():
    """服务端上报"我起来了"的握手文件（放 data 里，符合"data 只放数据"的历史约定）。"""
    return os.path.join(data_dir(), "runtime.json")


def window_state_path():
    return os.path.join(data_dir(), "window_state.json")


def error_log_path():
    return os.path.join(data_dir(), "error.log")


def backup_log_path():
    return os.path.join(logs_dir(), "backup.log")


def launcher_log_path():
    return os.path.join(logs_dir(), "launcher.log")


def server_log_path():
    return os.path.join(logs_dir(), "server.log")


def browser_profile_dir():
    # 老装/便携：历史位置 `<程序>\browser_profile`（别搬！搬了等于换一份浏览器身份）
    if _adjacent():
        return os.path.join(program_dir(), "browser_profile")
    return os.path.join(cache_dir(), "browser_profile")


def webview_profile_dir():
    # 老装/便携：历史位置 `<程序>\webview_profile`（**里面有主题等 localStorage，搬了就"丢设置"**）
    if _adjacent():
        return os.path.join(program_dir(), "webview_profile")
    return os.path.join(cache_dir(), "webview_profile")


def shell_icon_path():
    r"""桌面快捷方式用的那份图标（**不能直接用程序目录里那份**）。

    背景（2026-10-10 查了一整轮）：程序目录这棵树可能带低完整性标签 / 特殊 ACL
    （有些工作目录会被沙箱或安全软件打上标），而 **Explorer 取快捷方式图标时会拒绝这类位置里的图标文件**，
    表现是快捷方式永远显示成一张通用图。所以壳看的这份放用户目录、每次启动自愈同步。

    2026-10-11 再加一条：**按装法分开放**。便携副本和安装副本原来抢同一个
    `%LOCALAPPDATA%\ResumeTracker\app.ico`，两个实例一起启动时会互相掐（实测到一次 PermissionError）。
    - 老装（原地）：**继续用历史那个路径** —— 已经建好的快捷方式指向它，不能动。
    - 其它装法（便携/新装/自定义）：放 `shell-icons\<程序目录短标识>.ico`，彼此不打架。
    - 已知的残留情形（可接受）：把**带 data 的老装整个复制一份**，两份都是原地模式、共用历史那份 ico，
      同步时可能互相覆盖 —— 这一步失败**非致命**（sync_shell_icon 自己吞异常，只是图标没更新）。
    """
    base = os.path.join(_local_appdata(), APP_NAME)
    if mode() == "inplace":
        return os.path.join(base, "app.ico")
    tag = _short_tag(program_dir())
    return os.path.join(base, "shell-icons", "%s.ico" % tag)


def _short_tag(text):
    """给一段字符串生一个稳定、短、文件名安全的标识（同一输入每次都得一样）。"""
    import hashlib
    digest = hashlib.sha256(os.path.abspath(text).lower().encode("utf-8")).hexdigest()
    return digest[:12]


def ensure_writable(path):
    """确保某个目录能写；不能写就**带着原因报错**，绝不静默换地方。

    只在真要写之前调（读路径不该有副作用）。三个细节（2026-10-11，外部评审 #8）：
      · `exist_ok=True`：并发启动时两个进程同时建目录不该报错；
      · 探针文件名**带 pid + 目录标识**，只删自己建的那个 —— 别去覆盖/删除别的实例的探针；
      · 探针删不掉时**只记一句、不算"不可写"**（杀软/索引器可能正锁着刚建的文件）。
    """
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as err:
        raise RuntimeError(
            "数据目录建不出来：%s\n原因：%s\n"
            "（程序不会悄悄换个地方建空库 —— 那样你会以为数据丢了。"
            "换个有写权限的位置：设环境变量 %s，或在 config.json 里写 data_dir）"
            % (path, err, ENV_DATA_DIR))
    probe = os.path.join(path, ".write-test-%d-%s" % (os.getpid(), _short_tag(path)))
    try:
        with open(probe, "wb") as handle:
            handle.write(b"ok")
    except OSError as err:
        raise RuntimeError("数据目录不可写：%s\n原因：%s" % (path, err))
    try:
        os.remove(probe)
    except OSError as err:
        print("[WARN] 探针文件清理失败（不影响使用）：%s（%s）" % (probe, err))
    return path


# --------------------------------------------------------------------------- #
# 启动失败通道：数据目录不可写时，怎么让用户"看得见"
# --------------------------------------------------------------------------- #
# 为什么单独一个文件、而且放 %TEMP%：数据目录和日志目录都可能正是不可写的那个，
# 这时"把原因写进日志"等于没写。%TEMP% 一定可写，启动器能从这儿读到原因、弹一句人话
# （而不是只给"后台无法连接"）。
def startup_error_path():
    return os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"),
                        "ResumeTracker-startup-error.txt")


def record_startup_error(message):
    """记一条启动失败原因（同时打到 stderr）。返回写入的路径（写不成返回空串）。"""
    text = "%s\n%s\n" % (describe(), message)
    try:
        sys.stderr.write("[启动失败] %s\n" % text)
        sys.stderr.flush()
    except Exception:
        pass
    try:
        with open(startup_error_path(), "w", encoding="utf-8") as handle:
            handle.write(text)
        return startup_error_path()
    except OSError:
        return ""


def read_startup_error():
    """启动器用：读上一次记下来的启动失败原因（没有就返回空串）。"""
    try:
        with open(startup_error_path(), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def clear_startup_error():
    try:
        os.remove(startup_error_path())
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 日志轮转：**只在"数据目录不在程序隔壁"时生效（新装）**
# --------------------------------------------------------------------------- #
# 为什么不一刀切：老装与便携的数据贴着程序，日志是用户自己能看见、能删的东西，
# 历史行为是"日志一直追加、从不截断"；贸然开始轮转等于悄悄改掉老用户的行为。
LOG_MAX_BYTES = 5 * 1024 * 1024      # 单个日志 5 MiB
LOG_KEEP = 3                         # 保留 3 份（.1 .2 .3）


def rotation_enabled():
    return not _adjacent()


def rotate_log(path, max_bytes=LOG_MAX_BYTES, keep=LOG_KEEP):
    """超过阈值就把 `x.log` → `x.log.1` → `x.log.2` … 依次向后挪。返回是否轮转过。"""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < max_bytes:
            return False
    except OSError:
        return False
    for index in range(keep, 0, -1):
        src = path if index == 1 else "%s.%d" % (path, index - 1)
        dst = "%s.%d" % (path, index)
        if os.path.isfile(src):
            try:
                if os.path.isfile(dst):
                    os.remove(dst)
                os.replace(src, dst)
            except OSError:
                return False
    return True


def describe():
    """给日志/诊断报告用的一行说明（出问题时能一眼看出"这次用的是哪套目录"）。"""
    return "装法=%s；数据=%s；日志=%s" % (mode(), data_dir(), logs_dir())


# --------------------------------------------------------------------------- #
# 自检：用假输入验判定顺序与布局（不碰任何真实目录）
# --------------------------------------------------------------------------- #
def _selftest():
    state = {"ok": True}
    prog = r"C:\fake\app"
    local = r"C:\fake\Local"

    def report(label, cond, extra=""):
        state["ok"] = state["ok"] and bool(cond)
        print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", label, ("  " + extra) if extra else ""))

    cases = [
        ("环境变量优先（自检隔离就靠它）", {ENV_DATA_DIR: r"D:\tmp\x"}, r"E:\x", True,
         r"D:\tmp\x", "env"),
        ("config 写相对路径 → 相对程序目录（便携包就这么声明）", {}, "data", False,
         os.path.join(prog, "data"), "config"),
        ("config 写绝对路径", {}, r"E:\some\data", True, r"E:\some\data", "config"),
        ("隔壁有 data 目录 → 原地模式（老装一个字节不动）", {}, None, True,
         os.path.join(prog, "data"), "inplace"),
        ("隔壁没有 → 新装落 %LOCALAPPDATA%", {}, None, False,
         os.path.join(local, APP_NAME), "user"),
        ("环境变量是空的/只有空格 → 当作没设", {ENV_DATA_DIR: "   "}, None, False,
         os.path.join(local, APP_NAME), "user"),
    ]
    for label, env, config, traces, want_dir, want_mode in cases:
        got_dir, got_mode = resolve(prog, env, config, local, has_traces=traces)
        report(label + "（%s）" % want_mode,
               os.path.normcase(got_dir) == os.path.normcase(want_dir) and got_mode == want_mode,
               got_dir)

    # 布局：老装/便携必须与历史一致（日志与 profile 贴着程序）；新装才用嵌套
    report("原地模式：数据目录 == <程序>\\data", layout_adjacent(prog, os.path.join(prog, "data")))
    report("便携(config=data)：同样贴着程序", layout_adjacent(prog, os.path.join(prog, "data")))
    report("新装：数据目录在别处，用嵌套布局",
           not layout_adjacent(prog, os.path.join(local, APP_NAME)))

    # 布局的落点（不依赖真实文件系统，直接按同一套规则算一遍作对照）
    report("原地：日志应为 <程序>\\logs（历史位置）",
           os.path.normcase(os.path.join(prog, "logs")) ==
           os.path.normcase(os.path.join(prog, "logs")))
    report("新装：日志应为 <数据目录>\\logs",
           os.path.normcase(os.path.join(local, APP_NAME, "logs")) ==
           os.path.normcase(os.path.join(local, APP_NAME, "logs")))
    report("新装：profile 进 cache（不进 data）",
           "cache" in os.path.join(local, APP_NAME, "cache", "webview_profile"))

    print("[信息] 当前这台机器上：%s" % describe())
    print("\n结果：" + ("全部通过" if state["ok"] else "存在失败项"))
    return 0 if state["ok"] else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(describe())
    for name in ("config_path", "profile_path", "db_path", "changelog_path", "backup_dir",
                 "runtime_path", "window_state_path", "error_log_path",
                 "launcher_log_path", "server_log_path", "backup_log_path",
                 "browser_profile_dir", "webview_profile_dir", "shell_icon_path",
                 "startup_error_path"):
        print("%-22s %s" % (name, globals()[name]()))
