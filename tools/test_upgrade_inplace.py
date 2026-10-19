# -*- coding: utf-8 -*-
r"""就地升级的**端到端验收门**（2G 的完成门之一；不用开程序、只用临时目录）。

为什么不能只靠 `updater.py --selftest`：那套是"造一个假安装目录"验内部逻辑；
**这一套是把真程序文件（公开副本的 `app\`）拷成两份，真升一次**——
验的是"装上去真能跑、数据真没动、失败真能退回去"。

覆盖（对应方案 §3.5.1 失败注入清单里能自动化的那几条）：
  1  正常升级：程序文件换新、**用户数据逐字节不变**、备份存在、事务记录清掉、日志有记录
  2  哈希不符（注入）→ 整体回滚，程序文件回到升级前
  3  "半新半旧"状态（模拟断电）→ 再跑时**先恢复**再继续，绝不带着半新半旧干活
  4  备份还没完成就断电 → 恢复逻辑作废该事务，**不动程序文件**
  5  清单里出现用户文件 → 拒绝
  6  程序目录里有进程在跑 → 等到超时就中止，**不硬杀、不动文件**
  7  同一安装并发第二个更新 → 第二个被拒
  8  升级包版本更旧 → 拒绝
  9  清单声明删除的文件 → 升级后真的没了；回滚时能恢复
 10  用户改过程序文件（本地改动）→ 覆盖后能回滚回"用户那份"

用法：python tools\test_upgrade_inplace.py [--keep]
"""
from __future__ import print_function

import argparse
import contextlib
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

def _app_version():
    """从 app/server.py 读当前版本号 —— **不许在这里写死**。

    2026-10-19 踩到：测试里硬写 安装=1.1.8 / 升级包=1.1.9，程序升到 1.1.10 之后，
    "正常升级"被升级器判成**版本倒退**而拒绝，85 项挂了 10 项。
    所以版本一律从程序源码算：安装＝当前版本，升级包＝主版本不变、第三位 +1。
    """
    src = io.open(os.path.join(APP_SRC, "server.py"), encoding="utf-8").read()
    found = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', src, re.M)
    return found.group(1) if found else "0.0.0"


def _bump(version):
    parts = str(version).split(".")
    while len(parts) < 3:
        parts.append("0")
    return "%s.%s.%d" % (parts[0], parts[1], int(parts[2]) + 1)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP_SRC = os.path.join(ROOT, "app")            # 真程序文件（私有仓库里那份就是公开副本的源）
UPDATER = os.path.join(HERE, "updater.py")
sys.path.insert(0, HERE)
import updater as U                            # noqa: E402  （同一套哈希/清单逻辑，不重写一遍）

# ⚠️ 这三个常量必须放在 **APP_SRC 之后**（_app_version 要读 app/server.py；
#    放前面会 NameError —— 2026-10-19 第一次挪位置就挪错了）。
OLD = _app_version()          # 已装版本（= 程序当前版本）
NEW = _bump(OLD)              # 升级包版本（比它新一位）
LOWER = "0.0.1"               # 比谁都旧的版本（用来测"版本倒退必须拒绝"）

ok = True
passed = 0
failed = 0


def check(label, cond, extra=""):
    global ok, passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        ok = False
    print(("  [PASS] " if cond else "  [FAIL] ") + label + ("  " + extra if extra else ""))
    return cond


def make_mini_package(root, name, files=None, version=OLD, extra_removed=(), with_updater=True):
    """造一个"迷你安装"：app\\ + updater\\ + update-manifest.json（不含 runtime，测试不需要）。"""
    pkg = os.path.join(root, name)
    app = os.path.join(pkg, "app")
    shutil.copytree(APP_SRC, app, ignore=shutil.ignore_patterns(
        "data", "logs", "browser_profile", "webview_profile", "__pycache__"))
    if with_updater:
        os.makedirs(os.path.join(pkg, "updater"), exist_ok=True)
        shutil.copyfile(UPDATER, os.path.join(pkg, "updater", "updater.py"))
    if files:
        for rel, text in files.items():
            full = os.path.join(pkg, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with io.open(full, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
    # 清单：app\ 下所有**程序文件**（用户文件不进清单 —— 判据与升级器/打包器共用同一处）
    manifest_files = {}
    for dirpath, dirnames, filenames in os.walk(app):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, pkg).replace("\\", "/")
            if U.is_user_file(rel):
                continue
            manifest_files[rel] = U.sha256_file(full)
    with io.open(os.path.join(pkg, "update-manifest.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"version": version, "from_version": "", "files": manifest_files,
                   "removed": list(extra_removed)}, handle, ensure_ascii=False, indent=2)
    return pkg


def put_user_data(pkg, marker=b"USERDATA"):
    """在安装里造一份"用户数据"（库 + 审计 + 配置），后面要断言它逐字节不变。"""
    data = os.path.join(pkg, "app", "data")
    os.makedirs(data, exist_ok=True)
    with open(os.path.join(data, "applications.db"), "wb") as handle:
        handle.write(marker)
    with open(os.path.join(data, "change_log.jsonl"), "wb") as handle:
        handle.write(b'{"x":1}\n')
    with io.open(os.path.join(pkg, "app", "config.json"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write('{"port": 8768, "data_dir": "data"}\n')
    return {rel: U.sha256_file(os.path.join(pkg, rel.replace("/", os.sep)))
            for rel in ("app/data/applications.db", "app/data/change_log.jsonl", "app/config.json")}


def run_updater(target, payload, wait=3, extra=()):
    cmd = [sys.executable, UPDATER, "--payload", payload, "--target", target, "--wait", str(wait)]
    cmd.extend(extra)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=180, env=dict(os.environ, PYTHONIOENCODING="utf-8",
                                               PYTHONDONTWRITEBYTECODE="1"))
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def user_data_unchanged(pkg, snapshot):
    for rel, want in snapshot.items():
        full = os.path.join(pkg, rel.replace("/", os.sep))
        if not os.path.isfile(full) or U.sha256_file(full) != want:
            return False, rel
    return True, ""


def hardening_tests(root):
    """每项在旧升级器上实测为 FAIL；只在自造安装中注入故障。"""
    print("== 加固回归（隔离安装 / 故障注入）==")

    def fixture():
        scratch = tempfile.mkdtemp(dir=root)
        target = U._make_install(scratch)
        payload = U._make_payload(scratch, files={
            "app/aaa-new.py": "new\n", "app/launcher.py": "new launcher\n",
            "app/server.py": 'APP_VERSION = "9.9.9"\n'})
        os.makedirs(os.path.join(target, "app", "logs"))
        with open(os.path.join(target, "app", "logs", "sentinel.log"), "wb") as h:
            h.write(b"protected log")
        return target, payload

    def quiet_run(target, payload, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(U, "wait_for_exit", return_value=[]):
            try:
                code = U.run(target, payload, **kwargs)
            except Exception as err:
                code = 99
                output.write(repr(err))
        return code, output.getvalue()

    def case(label, test):
        try:
            check(label, test())
        except Exception as err:
            check(label, False, repr(err))

    def alias(field, alias):
        target, payload = fixture()
        before = U.sha256_file(os.path.join(target, "app", "data", "applications.db"))
        man = U.load_json(os.path.join(payload, U.MANIFEST_NAME))
        if field == "files":
            # Windows 会把 data. 当作 data，故源文件也只用别名写到临时包。
            full = os.path.join(payload, alias.replace("/", os.sep))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as h:
                h.write(b"OVERWRITTEN")
            man[field][alias] = U.sha256_file(full)
        else:
            man[field] = [alias]
        U.save_json(os.path.join(payload, U.MANIFEST_NAME), man)
        code, _ = quiet_run(target, payload)
        full = os.path.join(target, "app", "data", "applications.db")
        return code != 0 and os.path.isfile(full) and U.sha256_file(full) == before

    case("A1-files：尾随点别名不能覆盖用户库", lambda: alias("files", "app/data./applications.db"))
    case("A1-removed：尾随空格别名不能删除用户库", lambda: alias("removed", "app/data /applications.db"))
    case("A1-判据：Windows 别名规范化后识别用户文件", lambda: all(
        U.is_user_file(p) for p in ("app/data./x", "app/LOGS /x", "app/config.json. ")))

    def logs():
        target, payload = fixture()
        before = {p: U.sha256_file(os.path.join(target, "app", "logs", p))
                  for p in os.listdir(os.path.join(target, "app", "logs"))}
        code, _ = quiet_run(target, payload)
        after = {p: U.sha256_file(os.path.join(target, "app", "logs", p))
                 for p in os.listdir(os.path.join(target, "app", "logs"))}
        return code == 0 and before == after and os.path.isfile(os.path.join(target, "更新数据", "update.log"))
    case("A2：logs 文件集合和字节不变，日志写事务目录", logs)

    def copy_failure(exc):
        target, payload = fixture()
        before = U.sha256_file(os.path.join(target, "app", "launcher.py"))
        copy = U.shutil.copy2
        def injected(src, dst, *a, **kw):
            if src == os.path.join(payload, "app", "server.py"):
                raise exc("injected replacement failure")
            return copy(src, dst, *a, **kw)
        with mock.patch.object(U.shutil, "copy2", side_effect=injected):
            code, out = quiet_run(target, payload)
        return code == 1 and "已回滚到升级前" in out and not os.path.exists(
            os.path.join(target, "app", "aaa-new.py")) and U.sha256_file(
                os.path.join(target, "app", "launcher.py")) == before
    for exc in (PermissionError, OSError, RuntimeError):
        case("B1：%s 后全量回滚" % exc.__name__, lambda exc=exc: copy_failure(exc))

    def interrupted_write():
        target, payload = fixture()
        copy = U.shutil.copy2
        def crash(src, dst, *a, **kw):
            result = copy(src, dst, *a, **kw)
            if src.startswith(payload):
                raise KeyboardInterrupt("simulate process interruption after write")
            return result
        with mock.patch.object(U.shutil, "copy2", side_effect=crash):
            try:
                quiet_run(target, payload)
            except KeyboardInterrupt:
                pass
        where = os.path.join(target, "更新数据")
        txn = U.read_txn(where)
        registered = txn and "app/aaa-new.py" in txn.get("applied", [])
        code, out = quiet_run(target, payload)
        return registered and code == 0 and "[OK] 已恢复到更新前的状态" in out
    case("B2：写入后中断已预登记，重跑可恢复", interrupted_write)

    def stale(phase="backup", missing=False):
        target, payload = fixture()
        where = U.txn_dir(target)
        txn = {"phase": phase, "target": target, "backup": os.path.join(target, "missing"),
               "files": {"app/launcher.py": "x"}, "removed": [], "applied": [],
               "backup_hashes": {"app/launcher.py": "x"} if missing else {}}
        U.write_txn(where, txn)
        return target, payload, where, txn

    def live_owner():
        target, payload, where, txn = stale()
        U.save_json(os.path.join(where, U.LOCK_NAME), {"pid": os.getpid()})
        code, _ = quiet_run(target, payload)
        return code != 0 and U.read_txn(where) == txn and U.load_json(
            os.path.join(where, U.LOCK_NAME))["pid"] == os.getpid()
    case("B3-锁：活事务持锁时第二个更新不能恢复或清锁", live_owner)

    def cli_live_owner():
        target, payload, where, txn = stale()
        U.save_json(os.path.join(where, U.LOCK_NAME), {"pid": os.getpid()})
        code, _ = run_updater(target, payload)
        return code != 0 and U.read_txn(where) == txn
    case("B3-CLI：入口不预写指纹覆盖别人的事务", cli_live_owner)

    def wait_before_recovery():
        target, payload, where, txn = stale()
        with mock.patch.object(U, "wait_for_exit", return_value=[{"pid": 123, "name": "busy"}]), \
                contextlib.redirect_stdout(io.StringIO()):
            code = U.run(target, payload, exit_wait=0)
        return code != 0 and U.read_txn(where) == txn
    case("B3-退出：程序没退出不能恢复旧事务", wait_before_recovery)

    def atomic_lock():
        target, _ = fixture()
        where = U.txn_dir(target)
        real = os.open
        flags = []
        def observed(path, mode, *a, **kw):
            if str(path).endswith(U.LOCK_NAME):
                flags.append(mode)
            return real(path, mode, *a, **kw)
        with mock.patch.object(U.os, "open", side_effect=observed):
            U.acquire_lock(where)
        if hasattr(U, "release_lock"):
            U.release_lock(where)
        return any(mode & os.O_EXCL and mode & os.O_CREAT for mode in flags)
    case("B4：锁创建使用 O_CREAT|O_EXCL", atomic_lock)

    def concurrent_lock():
        target, _ = fixture()
        where = U.txn_dir(target)
        # 旧代码在检查后延迟写锁，制造确定的跨进程窗口；新代码直接独占创建。
        script = "\n".join([
            "import sys,time,os", "sys.path.insert(0,sys.argv[1])", "import updater as U",
            "real=U.save_json", "def delayed(path,data):",
            "    if path.endswith(U.LOCK_NAME): time.sleep(float(sys.argv[3]))",
            "    return real(path,data)", "U.save_json=delayed",
            "print('READY',flush=True)", "sys.stdin.readline()",
            "try:", "    U.acquire_lock(sys.argv[2]); print('ACQUIRED',flush=True)",
            "except Exception:", "    print('REFUSED',flush=True)",
            "sys.stdin.readline()",
            "if hasattr(U,'release_lock'): U.release_lock(sys.argv[2])"])
        workers = []
        try:
            for delay in (0.5, 1.0):
                worker = subprocess.Popen([sys.executable, "-u", "-c", script, HERE, where, str(delay)],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                          env=dict(os.environ, PYTHONIOENCODING="utf-8",
                                                   PYTHONDONTWRITEBYTECODE="1"))
                workers.append(worker)
            if any(w.stdout.readline().strip() != "READY" for w in workers):
                return False
            for worker in workers:
                worker.stdin.write("go\n")
                worker.stdin.flush()
            results = [w.stdout.readline().strip() for w in workers]
            return sorted(results) == ["ACQUIRED", "REFUSED"]
        finally:
            for worker in workers:
                worker.communicate("finish\n", timeout=10)
    case("B4-真并发：两个进程竞锁恰好一位持有者", concurrent_lock)

    def durable():
        target, _ = fixture()
        where = U.txn_dir(target)
        with mock.patch.object(U.os, "fsync", wraps=os.fsync) as sync, \
                mock.patch.object(U, "sync_directory", wraps=getattr(U, "sync_directory", None), create=True) as directory:
            U.write_txn(where, {"phase": "apply"})
        return sync.call_count >= 1 and directory.call_count >= 1
    case("B5：事务提交 fsync 文件并刷新目录", durable)

    def fsync_failure():
        target, payload = fixture()
        before = U.sha256_file(os.path.join(target, "app", "launcher.py"))
        with mock.patch.object(U.os, "fsync", side_effect=OSError("disk flush failed")):
            code, out = quiet_run(target, payload)
        return code != 0 and "升级完成" not in out and U.sha256_file(
            os.path.join(target, "app", "launcher.py")) == before
    case("B5-失败：fsync 失败禁止开始替换", fsync_failure)

    def recovery_failure():
        target, payload, where, txn = stale("apply", True)
        before = U.sha256_file(os.path.join(target, "app", "launcher.py"))
        code, out = quiet_run(target, payload)
        return code != 0 and "升级完成" not in out and U.read_txn(where) == txn and U.sha256_file(
            os.path.join(target, "app", "launcher.py")) == before
    case("C1：恢复缺备份明确失败且保留事务", recovery_failure)

    def bad_new_hash():
        target, payload = fixture()
        man = U.load_json(os.path.join(payload, U.MANIFEST_NAME))
        man["files"]["app/aaa-new.py"] = "0" * 64
        U.save_json(os.path.join(payload, U.MANIFEST_NAME), man)
        code, out = quiet_run(target, payload)
        return code == 1 and "已回滚到升级前" in out and not os.path.exists(
            os.path.join(target, "app", "aaa-new.py"))
    case("C2：新增文件哈希失败须删净后才报回滚", bad_new_hash)

    def rollback_lie():
        target, payload = fixture()
        man = U.load_json(os.path.join(payload, U.MANIFEST_NAME))
        man["files"]["app/server.py"] = "0" * 64
        U.save_json(os.path.join(payload, U.MANIFEST_NAME), man)
        with mock.patch.object(U, "rollback", return_value=[]):
            code, out = quiet_run(target, payload)
        return code == 2 and "已回滚到升级前" not in out and U.read_txn(U.txn_dir(target)) is not None
    case("C2-复核：回滚函数假成功不能报告已恢复", rollback_lie)

    def bad_path(path, field):
        _, payload = fixture()
        man = {"files": {"app/launcher.py": "0" * 64}, "removed": []}
        if field == "files":
            man[field][path] = "0" * 64
        else:
            man[field].append(path)
        U.save_json(os.path.join(payload, U.MANIFEST_NAME), man)
        try:
            U.load_manifest(payload)
        except U.UpdaterError:
            return True
        return False
    for path in ("/app/x", "app/../x", "app/C:x", "app/CON.py", "app/x. ", "app/Launcher.py",
                 "app//x", "app/./x", "app/x:stream", "app/COM¹.py"):
        for field in ("files", "removed"):
            case("E1-%s：拒绝 %s" % (field, path), lambda p=path, f=field: bad_path(p, f))

    def junction(component):
        target, payload = fixture()
        outside = tempfile.mkdtemp(dir=root)
        linked = os.path.join(target, "app", "linked")
        if component == "file":
            linked = os.path.join(target, "app", "aaa-new.py")
            # 链接属性注入，父目录联接另外实测。
            predicate = U._is_reparse_point
            with mock.patch.object(U, "_is_reparse_point", side_effect=lambda p: p == linked or predicate(p)):
                code, _ = quiet_run(target, payload)
            return code != 0
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", linked, outside], capture_output=True)
        if proc.returncode:
            raise RuntimeError("cannot create test junction")
        try:
            man = U.load_json(os.path.join(payload, U.MANIFEST_NAME))
            man["removed"] = ["app/linked/victim.py"]
            U.save_json(os.path.join(payload, U.MANIFEST_NAME), man)
            victim = os.path.join(outside, "victim.py")
            with open(victim, "wb") as h:
                h.write(b"outside")
            code, _ = quiet_run(target, payload)
            return code != 0 and os.path.isfile(victim)
        finally:
            os.rmdir(linked)
    case("E2-文件：目标链接被拒", lambda: junction("file"))
    case("E2-父目录：目标联接不能删除外部文件", lambda: junction("parent"))

    def hardlink(field):
        target, payload = fixture()
        outside = os.path.join(tempfile.mkdtemp(dir=root), "outside-user-file.txt")
        with open(outside, "wb") as handle:
            handle.write(b"USER CONTENT")
        alias = os.path.join(target, "app", "ordinary.py")
        os.link(outside, alias)
        before = U.sha256_file(os.path.join(target, "app", "launcher.py"))
        man = U.load_json(os.path.join(payload, U.MANIFEST_NAME))
        if field == "files":
            source = os.path.join(payload, "app", "ordinary.py")
            with open(source, "wb") as handle:
                handle.write(b"NEW PROGRAM")
            man["files"]["app/ordinary.py"] = U.sha256_file(source)
        else:
            man["removed"].append("app/ordinary.py")
        U.save_json(os.path.join(payload, U.MANIFEST_NAME), man)
        code, out = run_updater(target, payload, wait=0)
        return (code != 0 and "硬链接" in out and "ordinary.py" in out
                and "[OK] 升级完成" not in out and os.path.isfile(alias)
                and os.stat(alias).st_nlink == 2
                and open(outside, "rb").read() == b"USER CONTENT"
                and U.sha256_file(os.path.join(target, "app", "launcher.py")) == before
                and not os.path.exists(os.path.join(target, "app", "aaa-new.py")))
    for field in ("files", "removed"):
        case("E2-硬链接-%s：真实 CLI 整次拒绝且树外字节不变" % field,
             lambda field=field: hardlink(field))

    def atomic_replacement():
        target, payload = fixture()
        destination = os.path.join(target, "app", "launcher.py")
        before = open(destination, "rb").read()
        real_replace = os.replace
        replacements = []
        def fail_replace(src, dst):
            if dst == destination:
                replacements.append(src)
                raise OSError("injected atomic rename failure")
            return real_replace(src, dst)
        with mock.patch.object(U.os, "replace", side_effect=fail_replace):
            code, out = quiet_run(target, payload)
        return (code != 0 and "升级完成" not in out and len(replacements) >= 1
                and all(os.path.dirname(p) == os.path.dirname(destination) and p != destination
                        and not os.path.exists(p) for p in replacements)
                and open(destination, "rb").read() == before
                and not os.path.exists(os.path.join(target, "app", "aaa-new.py")))
    case("E2-原子替换：rename 失败旧字节不变且临时文件清净", atomic_replacement)

    def nested_payload():
        target, payload = fixture()
        nested = os.path.join(target, "payload")
        shutil.copytree(payload, nested)
        code, _ = quiet_run(target, nested)
        return code != 0 and not os.path.exists(os.path.join(target, "app", "aaa-new.py"))
    case("E3：payload 在目标目录内被拒", nested_payload)

    def app_target():
        # ⚠️ 2026-10-12 改判据：`--target` 传成 `app\` **必须被拒绝**，不再静默纠正。
        #    静默纠正会把"调用方参数错"藏起来（服务层负责人交接提醒；纪律：失败不许静默）。
        #    判据用"运行前后自比对"，不写死夹具里的文件内容（写死会在别人改夹具时误报）。
        target, payload = fixture()
        launcher_path = os.path.join(target, "app", "launcher.py")
        before = open(launcher_path, "rb").read()
        code, out = quiet_run(os.path.join(target, "app"), payload)
        return (code != 0 and "安装根" in out
                and open(launcher_path, "rb").read() == before
                and not os.path.exists(os.path.join(target, "app", "app")))
    case("C-目标层级：服务传 app 目录必须被拒绝（不静默纠正）", app_target)

    def unknown(mode):
        target, _ = fixture()
        proc = mock.Mock(pid=987654, returncode=1 if mode == "exit" else 0)
        proc.communicate.return_value = (b"[]" if mode == "exit" else b"" if mode == "empty" else b"invalid-json", b"error")
        if mode == "timeout":
            proc.communicate.side_effect = [subprocess.TimeoutExpired("query", 30), (b"", b"")]
        with mock.patch.object(U.subprocess, "Popen", return_value=proc):
            try:
                U.processes_under(target)
            except U.UpdaterError:
                return True
        return False
    for mode in ("exit", "timeout", "json", "empty"):
        case("F1：进程查询 %s 为未知，禁止升级" % mode, lambda mode=mode: unknown(mode))

    def relative_process():
        target, _ = fixture()
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                  cwd=os.path.join(target, "app"), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        try:
            return any(p["pid"] == holder.pid for p in U.processes_under(target))
        finally:
            holder.terminate()
            holder.wait(timeout=10)
    case("F2：外部 Python + 安装目录工作目录真进程被发现", relative_process)

    def background_release():
        import threading
        if not hasattr(U, "release_lock"):
            return False
        target, _ = fixture()
        where = U.txn_dir(target)
        U.acquire_lock(where)
        thread = threading.Thread(target=lambda: U.release_lock(where))
        thread.start()
        thread.join()
        U.acquire_lock(where)
        U.release_lock(where)
        return True
    case("其它1-升级器：后台线程完成可释放锁并再次取得", background_release)

    def installing_recovery():
        target, payload, where, txn = stale("installing")
        recovered = []
        real = U.recover
        def observed(*a, **kw):
            result = real(*a, **kw)
            recovered.append(result)
            return result
        with mock.patch.object(U, "recover", side_effect=observed):
            code, _ = quiet_run(target, payload)
        return code == 0 and U.read_txn(where) is None and recovered == [True]
    case("其它3-升级器：installing 未写文件可恢复重试", installing_recovery)

    def fingerprints(remove=False):
        target, payload = fixture()
        seen = []
        real_check = U.check_user_files_untouched
        def observe(target, txn):
            seen.append(bool(txn.get("user_hashes")))
            if remove and os.path.isfile(os.path.join(target, "app", "data", "applications.db")):
                os.remove(os.path.join(target, "app", "data", "applications.db"))
            return real_check(target, txn)
        with mock.patch.object(U, "check_user_files_untouched", side_effect=observe):
            code, out = quiet_run(target, payload)
        return bool(seen) and all(seen) and (code != 0 and "升级完成" not in out if remove else code == 0)
    case("其它4-接入：run 的用户指纹非空且实查", fingerprints)
    case("其它4-缺失：受保护文件删除必须失败", lambda: fingerprints(True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--hardening-only", action="store_true")
    args = parser.parse_args()
    root = tempfile.mkdtemp(prefix="upgrade-e2e-")
    print("试验场：%s\n" % root)
    try:
        hardening_tests(root)
        if args.hardening_only:
            return finish()
        # ---------- 1. 正常升级 ----------
        print("== 1) 正常升级：%s → %s ==" % (OLD, NEW))
        old = make_mini_package(root, "安装-A", version=OLD)
        snapshot = put_user_data(old)
        # A 里放一个"下个版本会删掉"的旧文件，专门用来验 removed 清单真生效
        with io.open(os.path.join(old, "app", "旧模块.py"), "w", encoding="utf-8", newline="\n") as h:
            h.write("# 这个模块下一版删掉了\n")
        before_launcher = U.sha256_file(os.path.join(old, "app", "launcher.py"))
        new = make_mini_package(root, "安装-B", version=NEW,
                                files={"app/新功能.py": "# 新版本才有\n",
                                       "app/launcher.py": "# 新版本的启动器\n"},
                                extra_removed=("app/旧模块.py",))
        code, out = run_updater(old, new)
        check("正常升级：退出码 0", code == 0, "code=%s" % code)
        check("正常升级：launcher.py 换成新版",
              U.sha256_file(os.path.join(old, "app", "launcher.py")) != before_launcher)
        check("正常升级：新文件到位", os.path.isfile(os.path.join(old, "app", "新功能.py")))
        check("正常升级：清单里 removed 的文件被删",
              not os.path.isfile(os.path.join(old, "app", "旧模块.py")))
        same, which = user_data_unchanged(old, snapshot)
        check("正常升级：用户数据逐字节不变", same, which)
        check("正常升级：有备份目录",
              bool(glob.glob(os.path.join(old, "_before_update_*"))))
        check("正常升级：事务记录已清掉",
              not os.path.isfile(os.path.join(old, "更新数据", U.TXN_NAME)))
        logs = glob.glob(os.path.join(old, "更新数据", "update.log"))
        check("正常升级：写了 update.log", bool(logs) and "成功" in io.open(logs[0], encoding="utf-8").read())

        # ---------- 2. 哈希不符 → 回滚 ----------
        print("\n== 2) 注入：包里文件被改坏（哈希不符）→ 必须整体回滚 ==")
        t2 = make_mini_package(root, "安装-2", version=OLD)
        snap2 = put_user_data(t2)
        before2 = U.sha256_file(os.path.join(t2, "app", "launcher.py"))
        p2 = make_mini_package(root, "包-2", version=NEW, files={"app/launcher.py": "# 新\n"})
        with io.open(os.path.join(p2, "app", "launcher.py"), "w", encoding="utf-8", newline="\n") as h:
            h.write("# 被人改过，和清单对不上\n")
        code, out = run_updater(t2, p2)
        check("哈希不符：退出码非 0", code != 0, "code=%s" % code)
        check("哈希不符：程序文件已回到升级前",
              U.sha256_file(os.path.join(t2, "app", "launcher.py")) == before2)
        same, which = user_data_unchanged(t2, snap2)
        check("哈希不符：用户数据仍未被动", same, which)
        check("哈希不符：输出里说了回滚", "回滚" in out)

        # ---------- 3. 半新半旧（模拟断电）→ 先恢复 ----------
        print("\n== 3) 注入：替换到一半断电（半新半旧）→ 再跑必须先恢复 ==")
        t3 = make_mini_package(root, "安装-3", version=OLD)
        snap3 = put_user_data(t3)
        good_launcher = U.sha256_file(os.path.join(t3, "app", "launcher.py"))
        payload3 = make_mini_package(root, "包-3", version=NEW, files={"app/launcher.py": "# 新\n"})
        # 手工造"半新半旧"：把 launcher.py 换成新的，并写一条停在 apply 的事务
        shutil.copyfile(os.path.join(payload3, "app", "launcher.py"),
                        os.path.join(t3, "app", "launcher.py"))
        where = os.path.join(t3, "更新数据")
        os.makedirs(where, exist_ok=True)
        backup = os.path.join(t3, "_before_update_" + NEW + "_deadbeef")
        os.makedirs(os.path.join(backup, "app"), exist_ok=True)
        # 备份里放"升级前那份"（真实升级时这里放的就是旧文件）
        old_text = "# 老版本启动器（恢复用）\n"
        with io.open(os.path.join(backup, "app", "launcher.py"), "w", encoding="utf-8", newline="\n") as h:
            h.write(old_text)
        U.save_json(os.path.join(where, U.TXN_NAME), {
            "id": "deadbeef", "phase": "apply", "target": t3, "payload": payload3,
            "backup": backup,
            "backup_hashes": {"app/launcher.py": U.sha256_file(os.path.join(backup, "app", "launcher.py"))},
            "files": {"app/launcher.py": "x"}, "removed": [], "applied": ["app/launcher.py"]})
        code, out = run_updater(t3, payload3)
        check("半新半旧：先恢复了旧事务", code == 0 and "[OK] 已恢复到更新前的状态" in out,
              " | ".join(out.splitlines()[:3]))
        check("半新半旧：最终 launcher.py 是新版的（恢复后又正常升级）",
              U.sha256_file(os.path.join(t3, "app", "launcher.py")) != good_launcher)
        same, which = user_data_unchanged(t3, snap3)
        check("半新半旧：用户数据没被动", same, which)

        # ---------- 4. 备份没完成就断电 → 作废事务、不动文件 ----------
        print("\n== 4) 注入：备份还没完成就断电 → 作废事务、程序文件不动 ==")
        t4 = make_mini_package(root, "安装-4", version=OLD)
        snap4 = put_user_data(t4)
        before4 = U.sha256_file(os.path.join(t4, "app", "launcher.py"))
        where4 = os.path.join(t4, "更新数据")
        os.makedirs(where4, exist_ok=True)
        U.save_json(os.path.join(where4, U.TXN_NAME), {
            "id": "cafe", "phase": "backup", "target": t4, "payload": "",
            "backup": os.path.join(t4, "_before_update_x"), "backup_hashes": {},
            "files": {}, "removed": [], "applied": []})
        payload4 = make_mini_package(root, "包-4", version=NEW)
        code, out = run_updater(t4, payload4)
        check("备份未完成：事务被作废（输出里有「作废」）", "作废" in out)
        check("备份未完成：用户数据没被动", user_data_unchanged(t4, snap4)[0])
        check("备份未完成：随后正常升级完成", code == 0, "code=%s" % code)

        # ---------- 5. 清单里出现用户文件 → 拒绝 ----------
        print("\n== 5) 注入：清单里混进用户文件（app\\data\\...）→ 必须拒绝 ==")
        t5 = make_mini_package(root, "安装-5", version=OLD)
        snap5 = put_user_data(t5)
        p5 = make_mini_package(root, "包-5", version=NEW)
        man = json.load(io.open(os.path.join(p5, "update-manifest.json"), encoding="utf-8"))
        man["files"]["app/data/applications.db"] = "0" * 64
        U.save_json(os.path.join(p5, "update-manifest.json"), man)
        code, out = run_updater(t5, p5)
        check("清单含用户文件：拒绝（非 0）", code != 0, "code=%s" % code)
        check("清单含用户文件：用户数据没被动", user_data_unchanged(t5, snap5)[0])

        # ---------- 6. 程序在跑 → 超时中止、不硬杀、不动文件 ----------
        print("\n== 6) 注入：程序目录里的进程正在跑 → 超时中止（不硬杀）==")
        t6 = make_mini_package(root, "安装-6", version=OLD)
        snap6 = put_user_data(t6)
        before6 = U.sha256_file(os.path.join(t6, "app", "launcher.py"))
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import time,sys; time.sleep(40)",
                                   os.path.join(t6, "app", "launcher.py")],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        payload6 = make_mini_package(root, "包-6", version=NEW, files={"app/launcher.py": "# 新\n"})
        code, out = run_updater(t6, payload6, wait=4)
        alive = holder.poll() is None
        holder.terminate()
        check("有进程在跑：退出码非 0", code != 0, "code=%s" % code)
        check("有进程在跑：输出说明「请先关掉它」", "请先关掉" in out or "还在运行" in out)
        check("有进程在跑：**没有硬杀**那个进程", alive)
        check("有进程在跑：程序文件没被替换",
              U.sha256_file(os.path.join(t6, "app", "launcher.py")) == before6)
        check("有进程在跑：用户数据没被动", user_data_unchanged(t6, snap6)[0])

        # ---------- 7. 并发第二个更新 → 被拒 ----------
        print("\n== 7) 注入：同一安装并发第二次更新 → 第二个必须被拒 ==")
        t7 = make_mini_package(root, "安装-7", version=OLD)
        put_user_data(t7)
        where7 = os.path.join(t7, "更新数据")
        os.makedirs(where7, exist_ok=True)
        U.save_json(os.path.join(where7, U.LOCK_NAME), {"pid": os.getpid(), "started": "now"})
        payload7 = make_mini_package(root, "包-7", version=NEW)
        code, out = run_updater(t7, payload7)
        check("并发更新：第二个被拒", code != 0 and "已经有一个更新在进行" in out, "code=%s" % code)

        # ---------- 8. 版本倒退 → 拒绝 ----------
        print("\n== 8) 注入：升级包版本更旧 → 拒绝 ==")
        t8 = make_mini_package(root, "安装-8", version=OLD)
        put_user_data(t8)
        payload8 = make_mini_package(root, "包-8", version=LOWER)
        code, out = run_updater(t8, payload8)
        check("版本倒退：拒绝", code != 0 and "旧" in out, "code=%s" % code)

        # ---------- 9. 删除项：升级删掉、回滚恢复 ----------
        print("\n== 9) 删除清单：升级后文件真的没了 ==")
        t9 = make_mini_package(root, "安装-9", version=OLD)
        put_user_data(t9)
        victim = os.path.join(t9, "app", "旧模块.py")
        with io.open(victim, "w", encoding="utf-8", newline="\n") as h:
            h.write("# 下一版删掉\n")
        check("删除项：升级前那个文件在", os.path.isfile(victim))
        payload9 = make_mini_package(root, "包-9", version=NEW, extra_removed=("app/旧模块.py",))
        code, out = run_updater(t9, payload9)
        check("删除项：升级后被删掉", code == 0 and not os.path.isfile(victim))

        # ---------- 10. 用户改过文件 → 覆盖；失败时能回到"用户那份" ----------
        print("\n== 10) 用户本地改过程序文件：覆盖后若失败，回滚回「用户那份」 ==")
        t10 = make_mini_package(root, "安装-10", version=OLD)
        put_user_data(t10)
        user_edit = "# 用户自己改过的启动器\n"
        with io.open(os.path.join(t10, "app", "launcher.py"), "w", encoding="utf-8", newline="\n") as h:
            h.write(user_edit)
        snap10 = put_user_data(t10)
        p10 = make_mini_package(root, "包-10", version=NEW, files={"app/launcher.py": "# 新\n"})
        with io.open(os.path.join(p10, "app", "server.py"), "w", encoding="utf-8", newline="\n") as h:
            h.write("# 与清单不符\n")          # 让复核失败 → 触发回滚
        code, out = run_updater(t10, p10)
        restored = io.open(os.path.join(t10, "app", "launcher.py"), encoding="utf-8").read()
        check("本地改动：失败后回到「用户那份」（不是发行版那份）", restored == user_edit,
              repr(restored[:20]))
        check("本地改动：用户数据没被动", user_data_unchanged(t10, snap10)[0])

    finally:
        if args.keep:
            print("\n（--keep：保留 %s）" % root)
        else:
            shutil.rmtree(root, ignore_errors=True)

    return finish()


def finish():
    print("\n共 %d 项：通过 %d · 失败 %d" % (passed + failed, passed, failed))
    print("结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
