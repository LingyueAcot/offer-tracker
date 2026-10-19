# -*- coding: utf-8 -*-
r"""更新服务离线门：真实状态文件/文件锁 + 假 opener/假 spawn + 临时安装监控。

python tools\test_update_service.py
所有数据库、缓存、安装目标均在 TemporaryDirectory；禁止真实网络。
"""
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from contextlib import contextmanager, ExitStack
from unittest.mock import patch

sys.dont_write_bytecode = True
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
from services import update_service as U, update_store as S, github_releases as G
import paths
import store as DB

passed = failed = 0


def check(label, condition, extra=""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
    print(("[PASS] " if condition else "[FAIL] ") + label + ("  " + extra if extra else ""))


class Response(io.BytesIO):
    def __init__(self, content, code=200):
        super().__init__(content)
        self.code = code
        self.headers = {"Content-Length": str(len(content)), "ETag": '"offline"'}

    def getcode(self):
        return self.code


class Opener:
    def __init__(self, fixture, code=200):
        self.fixture = fixture
        self.code = code
        self.calls = []

    def open(self, request, timeout):
        url = request.full_url
        self.calls.append(url)
        if url == G.API_LATEST:
            body = json.dumps(self.fixture.release).encode("utf-8")
        elif url.endswith("SHA256SUMS.txt"):
            body = (self.fixture.digest + "  " + self.fixture.name + "\n").encode()
        else:
            body = self.fixture.archive
        return Response(body, self.code)


class FakeProcess:
    def __init__(self, command, result_code=0, monitor_code=0, timeout=False):
        self.pid = os.getpid()
        self.returncode = monitor_code
        self.command = command
        self.result_code = result_code
        self.timeout = timeout

    def communicate(self, timeout):
        if self.timeout:
            raise subprocess.TimeoutExpired(self.command, timeout)
        with open(self.command[2], encoding="utf-8") as f:
            job = json.load(f)
        if not self.returncode:
            with open(job["result"], "w", encoding="utf-8") as f:
                json.dump({"token": job["token"], "code": self.result_code,
                           "timeout": False, "output": "offline installer output"}, f)
        return b"", b"offline monitor failure"


class Fixture:
    def __init__(self, base):
        self.base = base
        self.root = os.path.join(base, "install")
        self.program = os.path.join(self.root, "app")
        self.cache = os.path.join(base, "cache")
        self.data = os.path.join(base, "data")
        self.local = os.path.join(base, "LocalAppData")
        self.updater = os.path.join(self.root, "updater", "updater.py")
        for directory in (self.program, self.cache, self.data, os.path.dirname(self.updater)):
            os.makedirs(directory)
        self.name = U.rules.ASSET_ZIP_PREFIX + "1.2.0.zip"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as package:
            package.writestr("app/launcher.py", "new launcher")
        self.archive = buffer.getvalue()
        self.digest = hashlib.sha256(self.archive).hexdigest()
        self.release = {"tag_name": "v1.2.0", "draft": False, "prerelease": False,
                        "body": "offline notes", "html_url": "https://github.com/example/release",
                        "assets": []}
        prefix = "https://github.com/LingyueAcot/offer-tracker/releases/download/v1.2.0/"
        for name, size in ((self.name, len(self.archive)), (U.rules.CHECKSUM_ASSET_NAME, 120)):
            self.release["assets"].append({"name": name, "size": size, "state": "uploaded",
                                           "browser_download_url": prefix + name})
        self.commands = []
        self.spawn_options = {}
        self.process_options = {}
        self.opener = Opener(self)
        with open(self.updater, "w", encoding="utf-8", newline="\n") as f:
            f.write("print('offline updater')\n")

    def spawn(self, command, **options):
        self.commands.append(command)
        self.spawn_options = options
        return FakeProcess(command, **self.process_options)

    def configure(self):
        U.configure("1.1.0", self.program, updater_path=self.updater, spawn=self.spawn)

    def download(self):
        S.update_state(stage="found", known_version="1.2.0")
        U.start_download()
        wait_background()


def wait_background():
    for thread in threading.enumerate():
        if thread.name in {"update-download", "update-install"}:
            thread.join(10)
            if thread.is_alive():
                raise RuntimeError("后台任务未在 10 秒内结束")


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix="update-service-") as base, ExitStack() as stack:
        f = Fixture(base)
        S.release_lock()
        S._storage_dir = None
        stack.enter_context(patch.dict(os.environ, {"RESUME_TRACKER_DATA_DIR": f.data, "RESUME_TRACKER_NO_SHELL": "1",
                                                    "LOCALAPPDATA": f.local}))
        stack.enter_context(patch.object(paths, "program_dir", return_value=f.program))
        stack.enter_context(patch.object(paths, "cache_dir", return_value=f.cache))
        stack.enter_context(patch.object(paths, "data_dir", return_value=f.data))
        stack.enter_context(patch.object(U.tempfile, "gettempdir", return_value=base))
        stack.enter_context(patch.object(G, "_opener", side_effect=lambda *a: f.opener))
        # opener 已模拟；任何漏出的联网都必须让测试失败。
        stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("禁止真实联网")))
        f.configure()
        try:
            yield f
        finally:
            wait_background()
            S.release_lock()
            S._storage_dir = None


def test_check():
    with fixture() as f:
        first, second = U.check(), U.check()
        state = S.load_state()
        with open(S.state_path(), encoding="utf-8") as stream:
            disk = json.load(stream)
        check("3 连续自动检查只查一次网络且 stage 离开 checking",
              first["ok"] and second["http_status"] == 429 and len(f.opener.calls) == 1
              and state["stage"] == "found")
        check("3 last_check/next_allowed 真落盘且符合存储日期契约",
              bool(disk["last_check"]) and bool(disk["next_allowed"])
              and S._canonical_state(disk) == state
              and 86398 <= U._timestamp(disk["next_allowed"]) - U._timestamp(disk["last_check"]) <= 86400)
        check("3 manual 检查一分钟节流", U.check(True)["http_status"] == 429 and len(f.opener.calls) == 1)
        check("4 忽略后下载入口拒绝且不查询/不生成下载任务",
              U.ignore("1.2.0")["ok"] and U.start_download()["http_status"] == 409
              and len(f.opener.calls) == 1 and not U._operation.locked())
    with fixture() as f:
        f.opener.code = 429
        first, second = U.check(), U.check()
        state = S.load_state()
        check("3 检查失败退避真落盘且不重复联网",
              not first["ok"] and second["http_status"] == 429 and len(f.opener.calls) == 1
              and state["stage"] == "failed" and state["fail_count"] == 1
              and state["last_check"] and state["next_allowed"] and not state["last_check_ok"])


def test_download():
    with fixture() as f:
        directory = U._stage_dir("1.2.0", U._settings())
        for name in ("payload", "payload.tmp"):
            os.makedirs(os.path.join(directory, name))
            with open(os.path.join(directory, name, "old"), "w") as stream:
                stream.write("interrupted")
        for name in (f.name + ".part", U.rules.CHECKSUM_ASSET_NAME + ".part"):
            with open(os.path.join(directory, name), "wb") as stream:
                stream.write(b"partial")
        S.update_state(stage="downloading", known_version="1.2.0")
        response = U.start_download()
        wait_background()
        state = S.load_state()
        check("6 downloading 中断后可直接重试并清掉 .part/payload.tmp/旧 payload",
              response["ok"] and state["stage"] == "downloaded" and state["downloaded"] == "1.2.0"
              and not os.path.exists(os.path.join(directory, "payload.tmp"))
              and not os.path.exists(os.path.join(directory, f.name + ".part"))
              and not os.path.exists(os.path.join(directory, "payload", "old"))
              and open(os.path.join(state["staged_dir"], "app", "launcher.py")).read() == "new launcher")
        count = len(f.opener.calls)
        check("6 已下载版本不会重复下载", U.start_download()["http_status"] == 409 and len(f.opener.calls) == count)
        released = not U._operation.locked() and S._held_stream is None
        reserved = U._reserve()
        check("8 下载后台结束后两种锁均释放", released and reserved)
        if reserved:
            U._finish()
    with fixture() as f:
        f.digest = "a" * 64
        f.download()
        check("6 校验失败不会报 downloaded", S.load_state()["stage"] == "failed")
        f.digest = hashlib.sha256(f.archive).hexdigest()
        U.start_download()
        wait_background()
        check("6 校验失败后可直接再次下载", S.load_state()["stage"] == "downloaded")
    with fixture() as f:
        adjacent = os.path.join(f.program, "data")
        with patch.object(paths, "cache_dir", return_value=f.program), patch.object(paths, "data_dir", return_value=adjacent):
            f.download()
            check("其它发现2 老装/便携的外部缓存可用于锁、状态和下载",
                  S.load_state()["stage"] == "downloaded"
                  and not S._inside(S.state_path(), f.program)
                  and S.load_state()["staged_dir"].startswith(os.path.dirname(S.state_path())))


def test_install():
    for label, options, expected in (
        ("成功", {}, "installed"),
        ("更新器非零", {"result_code": 7}, "failed"),
        ("监控非零", {"monitor_code": 8}, "failed"),
        ("等待超时", {"timeout": True}, "failed"),
    ):
        with fixture() as f:
            f.download()
            f.process_options = options
            response = U.install()
            wait_background()
            state = S.load_state()
            check("2 安装%s等待结果并如实报告" % label,
                  response["stage"] == "installing" and state["stage"] == expected
                  and (bool(state["last_error"]) if expected == "failed" else state["installed_version"] == "1.2.0"))
            if label == "成功":
                with open(f.commands[0][2], encoding="utf-8") as stream:
                    job = json.load(stream)
                command = job["command"]
                check("1 --target 是安装根且 app 清单直达真实程序",
                      command[command.index("--target") + 1] == f.root
                      and os.path.join(command[-1], "app", "launcher.py") == os.path.join(f.program, "launcher.py"))
                check("2 退出码/输出被保留", state["install_exit_code"] == "0" and state["install_output"] == "offline installer output")
                check("2 安装完成的公开状态符合现有页面契约",
                      U.status()["stage"] == "idle" and U.status()["installed_version"] == "1.2.0")
                check("2 监控命令不被更新器误判为安装树内进程",
                      all(f.root.lower() not in arg.lower() for arg in f.commands[0]))
            check("8 安装后台%s后可重新取得锁" % label,
                  not U._operation.locked() and S._held_stream is None and U._reserve())
            U._finish()
    with fixture() as f:
        f.download()
        with patch.object(U, "_installer", side_effect=FileNotFoundError):
            response = U.install()
        check("2 缺更新器拒绝安装且释放锁", not response["ok"] and not f.commands and S._held_stream is None)
    with fixture() as f:
        f.download()
        with patch.object(U, "_prepare_monitor", side_effect=OSError("injected")):
            response = U.install()
        check("2 安装准备失败不会报成功", not response["ok"] and S.load_state()["stage"] == "failed")
    with fixture() as f:
        f.download()
        with patch.object(FakeProcess, "communicate", return_value=(b"", b"")):
            U.install()
            wait_background()
        check("2 监控退出零但缺完成结果仍报失败", S.load_state()["stage"] == "failed")
    with fixture() as f:
        f.download()
        runtime = os.path.join(f.root, "runtime")
        os.makedirs(runtime)
        executable = os.path.join(runtime, "python.exe")
        with open(executable, "wb") as stream:
            stream.write(b"offline runtime marker")
        command = U._prepare_monitor(U._settings(), S.load_state(), executable, f.updater, U.uuid.uuid4().hex)
        check("2 便携运行时复制到外部缓存避免阻断退出等待",
              U._outside(command[0], f.root) and open(command[0], "rb").read() == b"offline runtime marker")
    with fixture() as f:
        S._storage_dir = os.path.join(f.root, "更新数据", "update")
        os.makedirs(S._storage_dir)
        f.download()
        command = U._prepare_monitor(U._settings(), S.load_state(), sys.executable, f.updater, U.uuid.uuid4().hex)
        with open(command[2], encoding="utf-8") as stream:
            job = json.load(stream)
        payload = job["command"][job["command"].index("--payload") + 1]
        check("2 便携缓存落安装根内时将安装负载复制到外部",
              S.load_state()["stage"] == "downloaded" and U._outside(payload, f.root)
              and os.path.isfile(os.path.join(payload, "app", "launcher.py")))


def test_recovery():
    with fixture() as f:
        S.update_state(stage="installing", known_version="1.2.0")
        directory = os.path.join(f.root, "更新数据")
        os.makedirs(directory)
        with open(os.path.join(directory, "update-txn.json"), "w", encoding="utf-8") as stream:
            json.dump({"phase": "apply"}, stream)
        check("7 旧版 installing 无监控信息时恢复并报告事务阶段",
              U.status()["stage"] == "failed" and "apply" in U.status()["last_error"])
    for code, timed_out, expected in ((0, False, "installed"), (9, False, "failed"), (-1, True, "failed")):
        with fixture() as f:
            token = "a" * 32
            directory = U._monitor_dir(token)
            os.makedirs(directory)
            with open(os.path.join(directory, "result.json"), "w", encoding="utf-8") as stream:
                json.dump({"token": token, "code": code, "timeout": timed_out, "output": "recovered output"}, stream)
            S.update_state(stage="installing", known_version="1.2.0", install_token=token)
            f.configure()
            check("7 重启核对安装结果 code=%s timeout=%s" % (code, timed_out),
                  U.status()["stage"] == ("idle" if expected == "installed" else expected)
                  and S.load_state()["stage"] == expected and S.load_state()["install_output"] == "recovered output")
    with fixture() as f:
        S.update_state(stage="installing", known_version="1.2.0", install_token="b" * 32,
                       install_time=U._iso(time.time() - 60))
        check("7 中断且缺结果转 failed 并明确提醒未完成事务",
              U.status()["stage"] == "failed" and "未完成事务" in U.status()["last_error"])
        check("7 中断后可重试下载", U.start_download()["ok"])
        wait_background()
    with fixture() as f:
        _, started = S._process_info(os.getpid())
        S.update_state(stage="installing", known_version="1.2.0", install_token="c" * 32,
                       install_pid=str(os.getpid()), install_started=started, install_time=U._iso(time.time()))
        check("7 活监控进程保留 installing 且不接受新任务",
              U.status()["stage"] == "installing" and U.check()["http_status"] == 409
              and U.start_download()["http_status"] == 409 and not f.opener.calls)
        S.update_state(install_started="other process", install_time=U._iso(time.time() - 60))
        check("7 PID 复用不永久卡 installing", U.status()["stage"] == "failed")
    with fixture() as f:
        _, started = S._process_info(os.getpid())
        S.update_state(stage="failed", known_version="1.2.0", install_token="d" * 32,
                       install_pid=str(os.getpid()), install_started=started, install_time=U._iso(time.time()))
        check("7 监控等待超时但进程仍活着时禁止并发重试",
              U.check()["http_status"] == 409 and U.start_download()["http_status"] == 409
              and U.install()["http_status"] == 409 and not f.opener.calls)
    with fixture() as f:
        token = "e" * 32
        os.makedirs(U._monitor_dir(token))
        with open(os.path.join(U._monitor_dir(token), "result.json"), "w") as stream:
            stream.write("[]")
        S.update_state(stage="installing", known_version="1.2.0", install_token=token,
                       install_time=U._iso(time.time() - 60))
        check("7 损坏的完成结果不冒充成功、不阻塞恢复", U.status()["stage"] == "failed")


def test_monitor():
    # 真跑临时监控，但更新器是自造脚本；验证超时/非零而非只验假 spawn。
    for code, delay, timeout in ((0, 0, 5), (6, 0, 5), (0, 2, 0.1)):
        with fixture() as f:
            f.download()
            with open(f.updater, "w", encoding="utf-8", newline="\n") as stream:
                stream.write("import time,sys\nprint('real offline output', flush=True)\ntime.sleep(%s)\nsys.exit(%s)\n" % (delay, code))
            token = U.uuid.uuid4().hex
            with patch.object(U, "_INSTALL_TIMEOUT", timeout):
                command = U._prepare_monitor(U._settings(), S.load_state(), sys.executable, f.updater, token)
            proc = subprocess.run(command, capture_output=True, timeout=10)
            with open(os.path.join(U._monitor_dir(token), "result.json"), encoding="utf-8") as stream:
                receipt = json.load(stream)
            check("2 真监控采集退出码/输出/超时 code=%s timeout=%s" % (code, timeout),
                  proc.returncode == 0
                  and (receipt["timeout"] and receipt["code"] != 0 if timeout < 1 else
                       receipt["code"] == code and not receipt["timeout"] and "real offline output" in receipt["output"]))
    with fixture() as f:
        f.download()
        with open(f.updater, "w", encoding="utf-8") as stream:
            stream.write("import time\ntime.sleep(0.2)\nprint('survived parent')\n")
        token = U.uuid.uuid4().hex
        command = U._prepare_monitor(U._settings(), S.load_state(), sys.executable, f.updater, token)
        parent = "import subprocess,sys,json; subprocess.Popen(json.loads(sys.argv[1]),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
        subprocess.run([sys.executable, "-c", parent, json.dumps(command)], check=True, timeout=5)
        receipt = os.path.join(U._monitor_dir(token), "result.json")
        deadline = time.monotonic() + 5
        while not os.path.isfile(receipt) and time.monotonic() < deadline:
            time.sleep(0.05)
        check("2/7 后端父进程退出后监控仍保存安装完成结果",
              os.path.isfile(receipt) and json.load(open(receipt, encoding="utf-8"))["code"] == 0)


def test_database():
    with fixture() as f:
        db = os.path.join(f.data, "applications.db")
        DB.use(db)
        sqlite3.connect(db).close()
        check("5 表不存在与读取失败区分：新库返回 None", DB.read_schema_version() is None)
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.close()
        check("5 表存在但无记录返回 None", DB.read_schema_version() is None)
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO meta VALUES ('schema_version','99')")
        conn.close()
        check("5 高版本库读出真实版本", DB.read_schema_version() == 99)
        lock = sqlite3.connect(db)
        lock.execute("BEGIN EXCLUSIVE")
        raised = False
        try:
            with patch.object(DB, "CONNECT_TIMEOUT", 0.05):
                DB.read_schema_version()
        except sqlite3.Error:
            raised = True
        finally:
            lock.rollback()
            lock.close()
        check("5 锁定读取失败上报而不当成老库", raised)
        for label, body in (("高版本库", None), ("损坏库", b"not a sqlite database")):
            if body is not None:
                with open(db, "wb") as stream:
                    stream.write(body)
            before = open(db, "rb").read()
            env = dict(os.environ, RESUME_TRACKER_DATA_DIR=f.data, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
            # 子进程也离线：即使兼容拒绝回归、意外走到自动同步，也不允许联网/拉浏览器。
            wrapper = ("import socket,subprocess,runpy,sys,os\n"
                       "def blocked(*a, **k): raise RuntimeError('offline test: blocked')\n"
                       "socket.create_connection=socket.getaddrinfo=blocked\n"
                       "subprocess.Popen=blocked\n"
                       "sys.path.insert(0,os.path.dirname(sys.argv[1]))\n"
                       "runpy.run_path(sys.argv[1],run_name='__main__')\n")
            process = subprocess.run([sys.executable, "-c", wrapper, os.path.join(ROOT, "app", "server.py"), "--no-browser"],
                                     capture_output=True, env=env, timeout=15)
            check("5 %s拒绝启动返回 2 且未迁移库" % label,
                  process.returncode == 2 and open(db, "rb").read() == before,
                  "code=%s" % process.returncode)
        raised = False
        try:
            DB.read_schema_version()
        except sqlite3.Error:
            raised = True
        check("5 损坏库读取异常向调用方传播", raised)


def test_transaction_paths():
    from services import update_txn
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import updater
    with tempfile.TemporaryDirectory(prefix="txn-paths-") as base:
        with patch.dict(os.environ, {"LOCALAPPDATA": os.path.join(base, "local")}):
            for mode in ("老装", "新装", "便携"):
                install = os.path.join(base, mode)
                os.makedirs(os.path.join(install, "app"))
                if mode == "老装":
                    os.makedirs(os.path.join(install, "app", "data"))
                elif mode == "便携":
                    with open(os.path.join(install, "app", "config.json"), "w") as handle:
                        handle.write('{"data_dir":"data"}')
                for writable in (True, False):
                    with patch.object(os, "access", return_value=writable):
                        expected = (os.path.join(install, "更新数据") if writable else
                                    os.path.join(base, "local", "ResumeTracker", "更新数据"))
                        check("B5-路径：%s %s两端同址且只读不建目录" % (mode, "优先" if writable else "备用"),
                              update_txn.txn_dir(install) == updater.txn_dir(install, create=False) == expected
                              and not os.path.exists(expected))
                os.makedirs(os.path.join(install, "更新数据"))
                with patch.object(os, "access", return_value=False):
                    check("B5-路径：%s 已有本地事务不可写仍不隐藏" % mode,
                          update_txn.txn_dir(install) == updater.txn_dir(install, create=False)
                          == os.path.join(install, "更新数据"))
            fallback_install = os.path.join(base, "fallback")
            os.makedirs(fallback_install)
            with patch.object(os, "access", return_value=False):
                created = updater.txn_dir(fallback_install)
                check("B5-路径：备用目录实际创建与启动读址一致",
                      os.path.isdir(created) and created == update_txn.txn_dir(fallback_install))


def test_startup_transaction():
    """调用副本真实 main；在备份/迁移/自动联网边界观测，绝不接触真数据。"""
    with tempfile.TemporaryDirectory(prefix="startup-txn-") as base:
        install = os.path.join(base, "install")
        app = os.path.join(install, "app")
        for directory, dirs, names in os.walk(os.path.join(ROOT, "app")):
            dirs[:] = [d for d in dirs if d not in (
                "data", "logs", "browser_profile", "webview_profile", "__pycache__")]
            for name in names:
                if name.endswith(".py"):
                    relative = os.path.relpath(os.path.join(directory, name), os.path.join(ROOT, "app"))
                    destination = os.path.join(app, relative)
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    shutil.copyfile(os.path.join(directory, name), destination)
        where = os.path.join(install, "更新数据")
        os.makedirs(where)
        txn_path = os.path.join(where, "update-txn.json")
        data = os.path.join(base, "data")
        os.makedirs(data)
        db = os.path.join(data, "applications.db")
        with open(db, "wb") as handle:
            handle.write(b"SYNTHETIC USER DATA")
        wrapper = '''import sys,json,socket
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
import server
class Reached(Exception): pass
def stop(*a,**k): raise Reached()
socket.create_connection=socket.getaddrinfo=stop
with patch.object(server.backup,'run_startup_backup',return_value={}) as backup, patch.object(server.store,'read_schema_version',return_value=None) as read, patch.object(server.update_service,'maybe_auto_check',return_value=False) as auto, patch.object(server,'init_db',side_effect=stop) as init:
    try: code=server.main()
    except Reached: code=-1
    print('RESULT '+json.dumps({'code':code,'backup':backup.call_count,'read':read.call_count,'auto':auto.call_count,'init':init.call_count,'error':server.app_paths.read_startup_error()}))
'''
        for phase in ("apply", "verify", "rollback", "rollback-failed", "preflight", "backup"):
            transaction = {"phase": phase, "target": install, "applied": []}
            with open(txn_path, "w", encoding="utf-8") as handle:
                json.dump(transaction, handle)
            before = open(txn_path, "rb").read()
            env = dict(os.environ, RESUME_TRACKER_DATA_DIR=data, LOCALAPPDATA=os.path.join(base, "local"),
                       TEMP=base, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.run([sys.executable, "-c", wrapper, app], env=env, capture_output=True,
                                  text=True, encoding="utf-8", timeout=15)
            rows = [line[7:] for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
            result = json.loads(rows[-1]) if rows else {}
            safe = phase in ("preflight", "backup")
            expected = (result.get("code") == -1 and result.get("init") == 1
                        and "尚未替换" in proc.stdout) if safe else (
                        result.get("code") == 2 and all(result.get(k) == 0 for k in ("backup", "read", "auto", "init"))
                        and "上次升级没做完" in result.get("error", "")
                        and "一键升级.cmd" in result.get("error", "") and "你的数据没被动" in result.get("error", ""))
            check("B5-启动：%s %s且保留事务/数据" % (phase, "尚未替换可继续" if safe else "在备份/迁移前拒绝"),
                  proc.returncode == 0 and expected and open(txn_path, "rb").read() == before
                  and open(db, "rb").read() == b"SYNTHETIC USER DATA", "code=%s" % result.get("code"))
        # 记录坏了或看似早期却登记过替换，也不能推断为安全。
        from services import update_txn
        for label, body in (
                ("损坏 JSON", "{"), ("非对象", "[]"),
                ("未知阶段", json.dumps({"target": install, "phase": "unknown"})),
                ("目标不符", json.dumps({"target": base, "phase": "backup"})),
                ("backup 已登记替换", json.dumps({"target": install, "phase": "backup", "applied": ["app/x.py"]}))):
            with open(txn_path, "w", encoding="utf-8") as handle:
                handle.write(body)
            allowed, reason = update_txn.check_startup(app)
            check("B5-记录：%s 拒绝并保留现场" % label,
                  not allowed and "上次升级没做完" in reason and open(txn_path, encoding="utf-8").read() == body)


def test_lock():
    with fixture():
        acquired = U._reserve()
        thread = threading.Thread(target=U._finish)
        thread.start()
        thread.join(5)
        second = U._reserve()
        check("8 请求线程取得锁、后台线程结束后原生锁可再次取得",
              acquired and second and not thread.is_alive())
        if second:
            U._finish()
        check("8 完成后不再留有原生文件锁", S._held_stream is None and not U._operation.locked())


def main():
    for test in (test_check, test_download, test_install, test_recovery, test_monitor, test_database, test_transaction_paths, test_startup_transaction, test_lock):
        try:
            test()
        except Exception:
            check(test.__name__ + " 未预期异常", False)
            traceback.print_exc()
    print("共 %d 项：通过 %d · 失败 %d" % (passed + failed, passed, failed))
    print("结果：" + ("存在失败项" if failed else "全部通过"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
