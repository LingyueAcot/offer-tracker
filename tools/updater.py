# -*- coding: utf-8 -*-
r"""updater.py —— 就地升级程序（**事务化**：断电也能恢复；只碰程序文件）。

【它替掉的那个脚本有什么毛病】原来的 `one-click-upgrade.ps1`（106 行）有四处硬伤：
  ① 用**黑名单**（"除了 config/data/logs… 以外全复制"）→ 用户自己的文件会被覆盖；
  ② `$ErrorActionPreference = 'Continue'` → 复制失败也继续跑，最后照样说"升级完成"；
  ③ 检查只点名十几个文件，缺了别的照样"通过"；
  ④ **没有回滚**：失败只让你"自己把备份复制回去"。
新版按方案 §3.5.1 的结论重做：**白名单 + 事务记录 + 哈希复核 + 双向回滚 + 独立日志**。

【三条铁律】
  1. **只碰程序文件**：替换清单完全来自包里那份 `update-manifest.json`（白名单）。
     `data\`、`logs\`、`config.json`、`个人画像.json`、两个 profile 目录**既不在替换清单里，
     也不在回滚范围里** —— 替换期间它们必须逐字节不变。
  2. **绝不"半新半旧"地启动**：开始前写事务记录，每完成一步就落盘；任何一次运行都先看有没有
     未完成的事务，有就先恢复，恢复不了就明确失败并**保留现场**（绝不说"成功"）。
  3. **等真退出，不硬杀**：按"进程可执行文件或命令行落在安装目录下"判断，等到它们真的消失；
     超时就中止并让用户自己关程序（**不 taskkill**）。

【它自己放在哪】必须在**被替换的目录之外**（否则替换时会把正在运行的自己换掉/锁住）：
便携包里它在 `<包>\更新器\updater.py`，用 `<包>\runtime\python\python.exe` 跑，只替换 `<包>\app\`。

用法：
    <包>\runtime\python\python.exe 更新器\updater.py --payload <解开的升级包目录>
    python tools\updater.py --selftest                    # 反向验证（造各种坏情况，必须都拦住）
    python tools\updater.py --payload <目录> --dry-run     # 只演练，不落盘
"""
from __future__ import print_function

import argparse
import ctypes
import hashlib
import io
import json
import os
import ntpath
import shutil
import subprocess
import sys
import time
import threading
import tempfile
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

MANIFEST_NAME = "update-manifest.json"
TXN_NAME = "update-txn.json"
LOCK_NAME = "update.lock"
REMOVED_LOG = "update.log"
EXIT_WAIT_SECONDS = 60
# 绝不进入替换清单/回滚范围的用户文件（白名单之外的兜底断言）
USER_PATHS = ("data", "logs", "cache", "config.json", "个人画像.json", "startup-log.txt",
              "webview_profile", "browser_profile")
SAFE_PREFIXES = ("app/",)          # 只允许替换 app\ 下的东西 + 包根这几个脚本


def is_user_file(rel):
    """这个相对路径是不是**用户文件**（绝不许进更新清单、绝不许被替换/回滚）。

    打包器和升级器**共用这一个判据**：第一版打包器把 `app\\config.json` 打进了清单，
    升级器当场拒绝（拦对了），但清单本身是错的 —— 所以规则只能有一处（2026-10-11 端到端测试抓到）。
    """
    segments = [seg.rstrip(". ").casefold() for seg in str(rel).replace("\\", "/").split("/") if seg]
    return any(bad.lower() in segments for bad in USER_PATHS)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path, data):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name == "nt":
        # WRITE_THROUGH rename，再 FlushFileBuffers 目录句柄。
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
        move.restype = ctypes.c_int
        if not move(tmp, path, 0x1 | 0x8):  # REPLACE_EXISTING | WRITE_THROUGH
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(tmp, path)
    sync_directory(os.path.dirname(path))


def sync_directory(path):
    """文件系统目录屏障：POSIX fsync / Windows 原生 FlushFileBuffers。"""
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(path, 0x40000000, 7, None, 3, 0x02000000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.FlushFileBuffers(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)
    else:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def say(msg):
    print(msg, flush=True)


class UpdaterError(Exception):
    """带"阶段"的失败：写日志和事务记录时都要知道死在哪一步。"""

    def __init__(self, stage, message):
        Exception.__init__(self, message)
        self.stage = stage
        self.message = message


# --------------------------------------------------------------------------- #
# 清单 / 路径安全
# --------------------------------------------------------------------------- #
def load_manifest(payload_dir):
    path = os.path.join(payload_dir, MANIFEST_NAME)
    reject_links(path)
    if not os.path.isfile(path):
        raise UpdaterError("preflight", "升级包里没有 %s —— 这不是一个完整的升级包" % MANIFEST_NAME)
    data = load_json(path)
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise UpdaterError("preflight", "%s 里没有 files 清单" % MANIFEST_NAME)
    removed = data.get("removed") or []
    if not isinstance(removed, list):
        raise UpdaterError("preflight", "%s 的 removed 必须是数组" % MANIFEST_NAME)
    # ---- 白名单 + 路径安全（对应失败注入 #10）----
    seen = {}
    for rel in list(files) + list(removed):
        if not isinstance(rel, str) or not rel.strip():
            raise UpdaterError("preflight", "清单里有非法路径：%r" % (rel,))
        norm = validate_relative_path(rel)
        low = norm.casefold()
        if low in seen:
            raise UpdaterError("preflight", "清单里有大小写重名：%s / %s" % (seen[low], norm))
        seen[low] = norm
        # 用户文件绝不许出现在清单里（判据与打包器共用 is_user_file，只此一处）
        if is_user_file(norm):
            raise UpdaterError("preflight", "清单里出现了用户文件（绝不允许）：%s" % norm)
        if not any(norm.startswith(p) for p in SAFE_PREFIXES):
            raise UpdaterError("preflight", "清单里有白名单之外的路径：%s（只允许 %s）"
                               % (norm, "、".join(SAFE_PREFIXES)))
    data["files"] = {validate_relative_path(k): v for k, v in files.items()}
    data["removed"] = [validate_relative_path(r) for r in removed]
    return data


def check_payload_files(payload_dir, manifest):
    """包里该有的文件都在吗（先查再动，避免"替换到一半发现缺文件"）。"""
    missing = []
    for rel in manifest["files"]:
        full = os.path.join(payload_dir, rel.replace("/", os.sep))
        reject_links(full)
        if not os.path.isfile(full):
            missing.append(rel)
        elif os.path.islink(full) or _is_reparse_point(full):
            raise UpdaterError("preflight", "包里 %s 是链接（拒绝）：升级包必须是普通文件" % rel)
    if missing:
        raise UpdaterError("preflight", "升级包缺这些文件：%s" % "、".join(missing[:6]))


def _is_reparse_point(path):
    try:
        return bool(os.lstat(path).st_file_attributes & 0x400)      # FILE_ATTRIBUTE_REPARSE_POINT
    except (AttributeError, OSError):
        return False


def validate_relative_path(rel):
    """files / removed / 恢复记录共用，拒绝 Windows 的别名和设备路径。"""
    if not isinstance(rel, str) or not rel:
        raise UpdaterError("preflight", "非法路径：%r" % (rel,))
    norm = rel.replace("\\", "/")
    parts = norm.split("/")
    reserved = {"con", "prn", "aux", "nul", "conin$", "conout$"}
    reserved.update(prefix + digit for prefix in ("com", "lpt") for digit in "123456789¹²³")
    if ntpath.isabs(rel) or ntpath.splitdrive(rel)[0] or any(
            not part or part in (".", "..") or part != part.rstrip(". ") or
            any(ord(ch) < 32 or ch in '<>:"|?*' for ch in part) or
            part.split(".", 1)[0].casefold() in reserved for part in parts):
        raise UpdaterError("preflight", "清单里有非法或越界路径：%s" % rel)
    return norm


def reject_links(path):
    """不 resolve：逐级检查原路径，连安装目录本身和祖先的联接也拒绝。"""
    path = os.path.abspath(path)
    while True:
        if os.path.islink(path) or _is_reparse_point(path):
            raise UpdaterError("preflight", "路径或父目录是链接/联接（拒绝）：%s" % path)
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


def checked_path(root, rel):
    rel = validate_relative_path(rel)
    if is_user_file(rel) or not rel.startswith(SAFE_PREFIXES):
        raise UpdaterError("preflight", "拒绝替换/恢复非程序路径：%s" % rel)
    full = os.path.join(root, rel.replace("/", os.sep))
    reject_links(full)
    if os.path.exists(full) and not os.path.isfile(full):
        raise UpdaterError("preflight", "清单路径不是普通文件：%s" % rel)
    if os.path.exists(full) and os.stat(full).st_nlink > 1:
        raise UpdaterError("preflight", "目标文件是硬链接（多个名字共享内容，拒绝覆盖/删除）：%s" % full)
    return full


# --------------------------------------------------------------------------- #
# 事务记录 / 单实例
# --------------------------------------------------------------------------- #
def txn_dir(target, create=True):
    """与 app/services/update_txn.py 的只读目录契约一致（离线门逐装法核对）。"""
    local = os.path.join(os.path.abspath(target), "更新数据")
    ancestor = local
    while not os.path.exists(ancestor):
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            break
        ancestor = parent
    if os.path.lexists(local) or os.access(ancestor, os.W_OK):
        where = local
    else:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        where = os.path.join(base, "ResumeTracker", "更新数据")
    reject_links(where)
    if create:
        os.makedirs(where, exist_ok=True)
    return where


def write_txn(where, data):
    reject_links(os.path.join(where, TXN_NAME))
    reject_links(os.path.join(where, TXN_NAME + ".tmp"))
    save_json(os.path.join(where, TXN_NAME), data)


def read_txn(where):
    path = os.path.join(where, TXN_NAME)
    if not os.path.isfile(path):
        return None
    try:
        return load_json(path)
    except ValueError:
        return {"phase": "corrupt"}


def clear_txn(where):
    path = os.path.join(where, TXN_NAME)
    if os.path.exists(path):
        os.remove(path)
        sync_directory(where)


_locks = {}
_lock_guard = threading.Lock()


def acquire_lock(where):
    """独占创建 + 原生文件锁，锁文件保留，避免 unlink/重建造成两位持有者。"""
    import msvcrt
    path = os.path.join(where, LOCK_NAME)
    reject_links(path)
    with _lock_guard:
        if where in _locks:
            raise UpdaterError("preflight", "已经有一个更新在进行")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            fd = os.open(path, os.O_RDWR)
        stream = os.fdopen(fd, "r+b")
        locked = False
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
            raw = stream.read()
            metadata = json.loads(raw.decode("utf-8")) if raw else {}
            other = int(metadata.get("pid") or 0)
            if other and _pid_alive(other):
                raise UpdaterError("preflight", "已经有一个更新在进行（pid %d）" % other)
            stream.seek(0)
            stream.write(json.dumps({"pid": os.getpid(), "started": time.time()}).encode("utf-8"))
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
            sync_directory(where)
            _locks[where] = stream
        except Exception as err:
            if locked:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            stream.close()
            if isinstance(err, UpdaterError):
                raise
            raise UpdaterError("preflight", "已经有一个更新在进行或锁状态未知：%s" % err)


def release_lock(where):
    """锁归本次任务，允许完成任务的后台线程释放；绝不清掉别人的锁。"""
    import msvcrt
    with _lock_guard:
        stream = _locks.pop(where, None)
        if stream is None:
            return
        try:
            stream.seek(0)
            stream.write(b"{}")
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            try:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                stream.close()


def _pid_alive(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: 已不存在
            return False
        raise UpdaterError("preflight", "无法确认锁持有进程是否退出：pid %s" % pid)
    try:
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value == 259
    finally:
        kernel.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# 等真正的退出（进程绑定：命令行/可执行文件落在安装目录下）
# --------------------------------------------------------------------------- #
PS_QUERY = (
    "$ErrorActionPreference='Stop'; [Console]::OutputEncoding="
    "[System.Text.UTF8Encoding]::new($false); "
    "$items=@(Get-CimInstance Win32_Process | "
    "Select-Object ProcessId,Name,CreationDate,CommandLine,ExecutablePath); "
    "ConvertTo-Json -InputObject $items -Compress"
)


def processes_under(target, exclude_pids=()):
    """列出"落在这个安装目录下"的进程。返回 [{pid,name,created,cmd}]。

    ⚠️ **不按进程名判断**（方案 §3.5.1 第 1 条）：`pythonw.exe` 可能是别的程序的。
    绑定"命令行或可执行路径包含安装目录"，再加上进程创建时间，才不会误伤/误放。
    ⚠️ 还要**排除"这次查询用的那个 PowerShell"自己**：它的命令行里就带着安装目录，
    否则永远"有进程在跑"，替换永远等不到。（第一次跑反向验证时就是这么卡住的。）
    """
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_QUERY],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as err:
        raise UpdaterError("wait-exit", "进程查询无法启动，状态未知：%s" % err)
    try:
        out = proc.communicate(timeout=30)[0].decode("utf-8", "strict").strip()
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise UpdaterError("wait-exit", "进程查询超时，状态未知，中止升级")
    except UnicodeError as err:
        raise UpdaterError("wait-exit", "进程查询解码失败，状态未知：%s" % err)
    mine = list(exclude_pids) + [os.getpid(), proc.pid]
    if proc.returncode != 0 or not out:
        raise UpdaterError("wait-exit", "进程查询失败/空输出，状态未知，中止升级")
    try:
        data = json.loads(out)
    except ValueError as err:
        raise UpdaterError("wait-exit", "进程查询 JSON 解析失败，状态未知：%s" % err)
    if isinstance(data, dict):
        data = [data]
    out_list = []
    if not isinstance(data, list):
        raise UpdaterError("wait-exit", "进程查询结果不是数组，状态未知")
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("ProcessId"), int):
            raise UpdaterError("wait-exit", "进程查询记录不完整，状态未知")
        if item.get("ProcessId") in mine:
            continue
        cmd = item.get("CommandLine") or ""
        exe = item.get("ExecutablePath") or ""
        if not exe and (cmd or (item.get("Name") or "").casefold() in ("python.exe", "pythonw.exe")):
            try:
                exe = process_executable(item["ProcessId"])
            except OSError:
                if not _pid_alive(item["ProcessId"]):
                    continue
                raise UpdaterError("wait-exit", "进程可执行路径不可读，状态未知：pid %s" % item["ProcessId"])
        matches = os.path.normcase(target) in os.path.normcase(cmd) or path_under(exe, target)
        if not matches and exe:
            try:
                cwd = process_working_directory(item["ProcessId"])
            except OSError as err:
                # 已退出进程无须等待；仍活着又不可读则不能推断已退出。
                if _pid_alive(item["ProcessId"]):
                    raise UpdaterError("wait-exit", "进程工作目录不可读，状态未知：pid %s (%s)"
                                       % (item["ProcessId"], err))
                continue
            matches = path_under(cwd, target)
        elif not matches and not exe and cmd:
            raise UpdaterError("wait-exit", "进程可执行路径不可读，状态未知：pid %s" % item["ProcessId"])
        if not matches:
            continue
        out_list.append({"pid": item.get("ProcessId"), "name": item.get("Name"),
                         "created": str(item.get("CreationDate") or ""),
                         "cmd": (item.get("CommandLine") or "")[:200]})
    return out_list


def path_under(path, root):
    if not path:
        return False
    try:
        path, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def process_working_directory(pid):
    """CIM 不提供工作目录：只读进程 PEB，兼容这台电脑 64 位和 WOW64 32 位。"""
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    handle = kernel.OpenProcess(0x400 | 0x10, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ntdll = ctypes.WinDLL("ntdll")
        query = ntdll.NtQueryInformationProcess
        query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                          wintypes.ULONG, ctypes.c_void_p]
        query.restype = ctypes.c_long
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        basic = (ctypes.c_void_p * 6)()
        if query(handle, 0, basic, ctypes.sizeof(basic), None) != 0:
            raise OSError("NtQueryInformationProcess failed")
        peb = basic[1]
        if pointer_size == 8:
            wow = ctypes.c_void_p()
            if query(handle, 26, ctypes.byref(wow), ctypes.sizeof(wow), None) != 0:
                raise OSError("WOW64 process query failed")
            if wow.value:
                peb, pointer_size = wow.value, 4

        def read(address, size):
            buffer = ctypes.create_string_buffer(size)
            count = ctypes.c_size_t()
            if not kernel.ReadProcessMemory(handle, address, buffer, size, ctypes.byref(count)) or count.value != size:
                raise ctypes.WinError(ctypes.get_last_error())
            return buffer.raw

        params = int.from_bytes(read(peb + (0x20 if pointer_size == 8 else 0x10), pointer_size), "little")
        descriptor = read(params + (0x38 if pointer_size == 8 else 0x24), 16 if pointer_size == 8 else 8)
        length = int.from_bytes(descriptor[:2], "little")
        address = int.from_bytes(descriptor[8:] if pointer_size == 8 else descriptor[4:], "little")
        if not length or length % 2 or length > 32766 or not address:
            raise OSError("invalid process current directory")
        return read(address, length).decode("utf-16-le")
    finally:
        kernel.CloseHandle(handle)


def process_executable(pid):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value
    finally:
        kernel.CloseHandle(handle)


def wait_for_exit(target, seconds=EXIT_WAIT_SECONDS, on_tick=None):
    """等到没有进程落在安装目录下。超时**不硬杀**，直接报错让用户自己关。"""
    deadline = time.time() + seconds
    while True:
        running = processes_under(target)
        if not running:
            return []
        if time.time() >= deadline:
            return running
        if on_tick:
            on_tick(running)
        time.sleep(2)


# --------------------------------------------------------------------------- #
# 备份 / 替换 / 复核 / 回滚
# --------------------------------------------------------------------------- #
def atomic_copy(src, dst):
    """独占新建同目录临时文件再替换目录项，避免原地截断硬链接对象。"""
    reject_links(dst)
    if os.path.exists(dst) and os.stat(dst).st_nlink > 1:
        raise UpdaterError("preflight", "目标文件是硬链接（拒绝写入）：%s" % dst)
    fd, temporary = tempfile.mkstemp(prefix=".update-", suffix=".tmp", dir=os.path.dirname(dst))
    os.close(fd)
    try:
        shutil.copy2(src, temporary)
        with open(temporary, "r+b") as handle:
            os.fsync(handle.fileno())
        reject_links(dst)
        if os.path.exists(dst) and os.stat(dst).st_nlink > 1:
            raise UpdaterError("preflight", "目标文件变成硬链接（拒绝写入）：%s" % dst)
        os.replace(temporary, dst)
        sync_directory(os.path.dirname(dst))
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def backup_existing(target, backup_dir, rel_paths):
    """把"将被覆盖/将被删除"的文件先备份好，并逐个核对哈希（失败注入 #2：备份没完不进入替换）。"""
    manifest = {}
    for rel in rel_paths:
        src = checked_path(target, rel)
        if not os.path.isfile(src):
            continue
        dst = checked_path(backup_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        atomic_copy(src, dst)
        want, got = sha256_file(src), sha256_file(dst)
        if want != got:
            raise UpdaterError("backup", "备份核对不一致：%s" % rel)
        manifest[rel] = want
    return manifest


def apply_files(payload_dir, target, manifest, on_step=None):
    """按白名单替换。每替换一个就记一次（供断电后恢复用）。返回"已替换"的清单。"""
    done = []
    for rel, want_hash in sorted(manifest["files"].items()):
        src = checked_path(payload_dir, rel)
        dst = checked_path(target, rel)
        done.append(rel)
        if on_step:
            on_step(rel, done)  # 先持久化写入意图，失败/中断也能定位新增文件
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        atomic_copy(src, dst)
        got = sha256_file(dst)
        if got != want_hash:
            raise UpdaterError("apply", "写进去的文件哈希不符：%s" % rel)
    for rel in manifest["removed"]:
        dst = checked_path(target, rel)
        if os.path.isfile(dst):
            done.append(rel)
            if on_step:
                on_step(rel, done)
            os.remove(dst)
            sync_directory(os.path.dirname(dst))
    return done


def verify_all(target, manifest):
    bad = []
    for rel, want in manifest["files"].items():
        full = os.path.join(target, rel.replace("/", os.sep))
        if not os.path.isfile(full) or sha256_file(full) != want:
            bad.append(rel)
    for rel in manifest["removed"]:
        if os.path.lexists(os.path.join(target, rel.replace("/", os.sep))):
            bad.append(rel)
    return bad


def rollback(target, backup_dir, backup_manifest, applied, manifest):
    """**双向**回滚（方案 §3.5.1 第 4 条）：恢复被覆盖的、恢复被删的、**删掉本次新增的**。"""
    problems = []
    # ① 本次新增的文件要删掉
    for rel in applied:
        if rel in backup_manifest:
            continue
        try:
            full = checked_path(target, rel)
            if os.path.isfile(full):
                os.remove(full)
                sync_directory(os.path.dirname(full))
        except Exception as err:
            problems.append("删不掉新增文件 %s（%s）" % (rel, err))
    # ② 被覆盖 / 被删除的从备份恢复
    for rel, want_hash in backup_manifest.items():
        try:
            src = checked_path(backup_dir, rel)
            dst = checked_path(target, rel)
            if sha256_file(src) != want_hash:
                raise UpdaterError("rollback", "备份哈希不符：%s" % rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            atomic_copy(src, dst)
            if sha256_file(dst) != want_hash:
                problems.append("恢复后哈希不符：%s" % rel)
        except Exception as err:
            problems.append("恢复不了 %s（%s）" % (rel, err))
    return problems


def verify_rollback(target, txn):
    """独立复核，不能仅相信 rollback() 的返回值；缺失和新增均有断言。"""
    problems = []
    hashes = txn.get("backup_hashes") or {}
    for rel, want in hashes.items():
        try:
            full = checked_path(target, rel)
            if not os.path.isfile(full) or sha256_file(full) != want:
                problems.append("回滚复核不符：%s" % rel)
        except Exception as err:
            problems.append("回滚复核失败：%s (%s)" % (rel, err))
    for rel in txn.get("absent_before", txn.get("applied") or []):
        if rel not in hashes and os.path.lexists(os.path.join(target, rel.replace("/", os.sep))):
            problems.append("回滚仍残留新增文件：%s" % rel)
    try:
        check_user_files_untouched(target, txn)
    except Exception as err:
        problems.append(str(err))
    return problems


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def log_path(target):
    path = os.path.join(txn_dir(target, create=False), REMOVED_LOG)
    reject_links(path)
    return path


def write_log(path, lines):
    if not path:
        return
    try:
        with io.open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(target, payload_dir, dry_run=False, exit_wait=EXIT_WAIT_SECONDS):
    target = os.path.abspath(target)
    # ⚠️ **不要静默纠正**：`--target` 传成 `app\` 说明调用方把语义弄错了
    #    （清单里的 `app/...` 是相对**安装根**解析的）。静默纠正会把"参数错"藏起来，
    #    哪天清单前缀一变就会写到错地方还报成功（2026-10-12 服务层负责人交接提醒）。
    #    宁可当场拒绝，让调用方传对。
    if os.path.basename(target).casefold() == "app" and os.path.isfile(os.path.join(target, "launcher.py")):
        raise UpdaterError("preflight",
                           "--target 传成了 app\\ 目录；请传**安装根**（app\\ 的上一级）——"
                           "清单里的 app/... 是相对安装根解析的")
    payload_dir = os.path.abspath(payload_dir)
    where, log = None, ""
    locked, started_apply = False, False
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    txn = {"id": uuid.uuid4().hex[:8], "phase": "preflight", "target": target,
           "payload": payload_dir, "started": stamp}
    say("== 就地升级 ==")
    say("   安装目录：%s" % target)
    say("   升级包：  %s" % payload_dir)
    say("   事务号：  %s" % txn["id"])
    try:
        reject_links(target)
        reject_links(payload_dir)
        if path_under(payload_dir, target):
            raise UpdaterError("preflight", "升级包不能位于目标目录内")
        manifest = load_manifest(payload_dir)
        check_payload_files(payload_dir, manifest)
        for rel in list(manifest["files"]) + manifest["removed"]:
            checked_path(target, rel)
        if dry_run:
            if os.path.exists(os.path.join(txn_dir(target, create=False), TXN_NAME)):
                raise UpdaterError("preflight", "存在未完成事务，请先实际运行恢复")
            check_installed(target, manifest, say)
            say("   [演练] 清单检查通过：替换 %d 个、删除 %d 个（未落盘）"
                % (len(manifest["files"]), len(manifest["removed"])))
            return 0
        where = txn_dir(target)
        log = log_path(target)
        acquire_lock(where)
        locked = True
        say("   等程序退出（最多 %d 秒，不硬杀）……" % exit_wait)
        running = wait_for_exit(target, exit_wait,
                                on_tick=lambda r: say("     还在跑：%s（pid %s）"
                                                      % (r[0]["name"], r[0]["pid"])))
        if running:
            raise UpdaterError("wait-exit", "程序还在运行——请先关掉它再升级：%s" % running)
        # 不覆盖旧记录，锁和退出检查都通过后才允许触碰旧事务。
        stale = read_txn(where)
        if stale:
            if not isinstance(stale, dict) or os.path.normcase(os.path.abspath(stale.get("target") or "")) != os.path.normcase(target):
                raise UpdaterError("recover", "旧事务损坏或目标不符，保留现场")
            if not recover(where, stale, say):
                raise UpdaterError("recover", "旧事务无法自动恢复，保留现场并中止升级")
        check_installed(target, manifest, say)
        txn.update(version=manifest.get("version") or "", files=manifest["files"],
                   removed=manifest["removed"], user_hashes=snapshot_user_files(target))
        backup_dir = os.path.join(target, "_before_update_%s" % txn["id"])
        reject_links(backup_dir)
        txn.update(phase="backup", backup=backup_dir)
        write_txn(where, txn)
        os.makedirs(backup_dir, exist_ok=False)
        paths = list(manifest["files"]) + manifest["removed"]
        txn["backup_hashes"] = backup_existing(target, backup_dir, paths)
        txn["absent_before"] = [rel for rel in paths if rel not in txn["backup_hashes"]]
        write_txn(where, txn)
        say("   已备份 %d 个将被覆盖/删除的文件 → %s"
            % (len(txn["backup_hashes"]), os.path.basename(backup_dir)))
        txn.update(phase="apply", applied=[])
        write_txn(where, txn)
        started_apply = True

        def step(rel, done):
            txn["applied"] = list(done)
            write_txn(where, txn)

        applied = apply_files(payload_dir, target, manifest, step)
        say("   已替换/删除 %d 个程序文件" % len(applied))
        txn["phase"] = "verify"
        write_txn(where, txn)
        bad = verify_all(target, manifest)
        if bad:
            raise UpdaterError("verify", "复核发现 %d 个文件不符：%s" % (len(bad), "、".join(bad[:6])))
        check_user_files_untouched(target, txn)
        extra = extra_files(target, manifest)
        if extra:
            say("   [提示] 旧版本残留 %d 个文件（已保留不删）：%s" % (len(extra), "、".join(extra[:5])))
        txn.update(phase="done", finished=time.strftime("%Y-%m-%d %H:%M:%S"))
        write_txn(where, txn)
        clear_txn(where)
        say("   [OK] 升级完成：%s → %s（只换了程序文件）"
            % (manifest.get("from_version") or "?", manifest.get("version") or "?"))
        say("        备份留在：%s" % backup_dir)
        write_log(log, ["[%s] 事务 %s 成功；备份 %s" % (stamp, txn["id"], backup_dir)])
        return 0
    except Exception as err:
        stage = err.stage if isinstance(err, UpdaterError) else txn["phase"]
        reason = str(err)
        say("   [X] 升级失败（阶段：%s）：%s" % (stage, reason))
        if started_apply:
            txn.update(phase="rollback", failed_stage=stage, failed_reason=reason)
            # 即使记录落盘失败，也要尝试恢复正式文件；内存仍保存写入意图。
            try:
                write_txn(where, txn)
            except Exception as journal_error:
                say("   [X] 回滚记录落盘失败：%s" % journal_error)
            try:
                problems = rollback(target, txn["backup"], txn.get("backup_hashes") or {},
                                    txn.get("applied") or [], manifest)
            except Exception as rollback_error:
                problems = ["回滚异常：%s" % rollback_error]
            problems += verify_rollback(target, txn)
            if problems:
                txn.update(phase="rollback-failed", rollback_problems=problems)
                try:
                    write_txn(where, txn)
                except Exception as journal_error:
                    say("   [X] 现场记录失败：%s" % journal_error)
                say("   [X] 回滚没做干净，现场已保留：%s" % "；".join(problems))
                write_log(log, ["[%s] 失败且回滚不完整：%s" % (stamp, problems)])
                return 2
            clear_txn(where)
            say("   [OK] 已回滚到升级前（已逐个复核程序文件与用户指纹）")
            write_log(log, ["[%s] 事务 %s 失败(%s) 已回滚：%s" % (stamp, txn["id"], stage, reason)])
        else:
            # 退出未知/恢复失败不清旧事务。备份阶段还没写程序文件，保留供下次作废。
            write_log(log, ["[%s] 拒绝升级（%s）：%s" % (stamp, stage, reason)])
        return 1
    finally:
        if locked:
            release_lock(where)


def extra_files(target, manifest):
    """目标 app\\ 下有、但新清单里没有的文件（旧版残留）。只报告，不删。"""
    known = set(manifest["files"]) | set(manifest["removed"])
    known_lower = {k.lower() for k in known}
    out = []
    app = os.path.join(target, "app")
    if not os.path.isdir(app):
        return out
    for dirpath, dirnames, filenames in os.walk(app):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "data", "logs", "cache")]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, target).replace("\\", "/")
            if rel.lower() not in known_lower:
                out.append(rel)
    return out


def find_installs(roots=None, depth=3):
    """找"像是这套程序"的安装目录（旧版脚本也这么干）：有 app\\launcher.py，且有 data\\ 痕迹。"""
    if roots is None:
        home = os.path.expanduser("~")
        roots = [os.path.join(home, name) for name in
                 ("Desktop", "桌面", "Downloads", "Documents", "OneDrive/Desktop", "OneDrive/桌面")]
    hits = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath[len(root):].count(os.sep) >= depth:
                dirnames[:] = []
                continue
            if "launcher.py" in filenames and os.path.basename(dirpath).lower() == "app":
                install = os.path.dirname(dirpath)
                if install not in hits:
                    hits.append(install)
    return hits


def check_installed(target, manifest, say_fn):
    """装上之前先看：目标像不像一个安装、版本是不是更新的。"""
    if not os.path.isfile(os.path.join(target, "app", "launcher.py")) and \
       not os.path.isfile(os.path.join(target, "launcher.py")):
        raise UpdaterError("preflight", "%s 里没有 launcher.py —— 这不像程序目录" % target)
    inst = read_installed_version(target)
    new = (manifest.get("version") or "").strip()
    if inst and new and _cmp_version(new, inst) < 0:
        raise UpdaterError("preflight", "升级包版本（%s）比已装的（%s）还旧；"
                                        "如果你确实要装旧版，请用界面上的「重装这个版本」" % (new, inst))
    say_fn("   已装版本：%s；升级包版本：%s" % (inst or "（读不到）", new or "（未声明）"))


def read_installed_version(target):
    for candidate in (os.path.join(target, "app", "server.py"), os.path.join(target, "server.py")):
        if not os.path.isfile(candidate):
            continue
        try:
            with io.open(candidate, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if line.startswith("APP_VERSION"):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return ""


def _cmp_version(a, b):
    def parts(v):
        out = []
        for piece in (v or "").split("."):
            digits = "".join(ch for ch in piece if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return out
    pa, pb = parts(a), parts(b)
    for i in range(max(len(pa), len(pb))):
        x = pa[i] if i < len(pa) else 0
        y = pb[i] if i < len(pb) else 0
        if x != y:
            return 1 if x > y else -1
    return 0


def check_user_files_untouched(target, txn):
    """替换阶段结束时，用户文件必须逐字节没变（方案 §3.5.1 第 5 条的硬断言）。"""
    saved = (txn.get("user_hashes") or {})
    current = snapshot_user_files(target)
    if current != saved:
        changed = sorted(set(saved) ^ set(current) | {r for r in saved if r in current and saved[r] != current[r]})
        raise UpdaterError("verify", "用户文件缺失、增加或被动过了：%s" % "、".join(changed[:6]))


def snapshot_user_files(target):
    """替换前给用户文件拍哈希（用于上面的断言）。"""
    out = {}
    for base in (target, os.path.join(target, "app")):
        for name in USER_PATHS:
            full = os.path.join(base, name)
            reject_links(full)
            if os.path.isfile(full):
                out[os.path.relpath(full, target).replace("\\", "/")] = sha256_file(full)
            elif os.path.isdir(full):
                for folder, dirs, files in os.walk(full):
                    for child in dirs + files:
                        reject_links(os.path.join(folder, child))
                    for child in files:
                        item = os.path.join(folder, child)
                        out[os.path.relpath(item, target).replace("\\", "/")] = sha256_file(item)
    return out


# --------------------------------------------------------------------------- #
# 断点恢复
# --------------------------------------------------------------------------- #
def recover(where, txn, say_fn):
    """上次没做完：按事务记录恢复（对应失败注入 #3/#4）。"""
    target = txn.get("target") or ""
    backup_dir = txn.get("backup") or ""
    backup_hashes = txn.get("backup_hashes") or {}
    applied = txn.get("applied") or []
    say_fn("   恢复上次未完成的更新：阶段=%s" % txn.get("phase"))
    phase = txn.get("phase")
    if phase in ("preflight", "wait-exit", "backup", "installing") and not applied:
        say_fn("   （备份还没完成，程序文件没被改过 —— 直接作废这条事务）")
        clear_txn(where)
        return True
    if phase not in ("apply", "installing", "verify", "rollback", "rollback-failed", "done", "rolled-back"):
        say_fn("   [X] 未知事务阶段，保留现场")
        return False
    if not backup_dir or not os.path.isdir(backup_dir):
        say_fn("   [X] 备份目录不在了（%s）—— 无法自动恢复，保留现场" % backup_dir)
        return False
    paths = list(txn.get("files") or {}) + list(txn.get("removed") or []) + list(backup_hashes) + applied
    try:
        for rel in paths:
            checked_path(target, rel)
        reject_links(backup_dir)
        if not path_under(backup_dir, target) or not os.path.basename(backup_dir).startswith("_before_update_"):
            raise UpdaterError("recover", "备份路径不在安装的备份目录下")
        problems = rollback(target, backup_dir, backup_hashes, applied,
                            {"files": txn.get("files") or {}, "removed": txn.get("removed") or []})
        # 老事务没有指纹时采一份，恢复前后也必须一致。
        if "user_hashes" not in txn:
            txn["user_hashes"] = snapshot_user_files(target)
        problems += verify_rollback(target, txn)
    except Exception as err:
        say_fn("   [X] 恢复失败，保留现场：%s" % err)
        return False
    if problems:
        say_fn("   [X] 恢复不完整：%s" % "；".join(problems))
        return False
    clear_txn(where)
    say_fn("   [OK] 已恢复到更新前的状态")
    return True


# --------------------------------------------------------------------------- #
# 反向验证：造各种坏情况，必须都拦住（坏情况一个都不能漏）
# --------------------------------------------------------------------------- #
def _make_payload(root, version="9.9.9", files=None, removed=None):
    payload = os.path.join(root, "payload")
    os.makedirs(os.path.join(payload, "app"), exist_ok=True)
    content = files if files is not None else {"app/launcher.py": "print('new')\n",
                                               "app/server.py": "APP_VERSION = \"9.9.9\"\n"}
    hashes = {}
    for rel, text in content.items():
        full = os.path.join(payload, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with io.open(full, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        hashes[rel] = sha256_file(full)
    save_json(os.path.join(payload, MANIFEST_NAME),
              {"version": version, "from_version": "1.0.0", "files": hashes,
               "removed": removed or []})
    return payload


def _make_install(root, version="1.0.0"):
    target = os.path.join(root, "install")
    os.makedirs(os.path.join(target, "app", "web"), exist_ok=True)
    with io.open(os.path.join(target, "app", "launcher.py"), "w", encoding="utf-8", newline="\n") as h:
        h.write("print('old')\n")
    with io.open(os.path.join(target, "app", "server.py"), "w", encoding="utf-8", newline="\n") as h:
        h.write("APP_VERSION = \"%s\"\n" % version)
    with io.open(os.path.join(target, "app", "data.json"), "w", encoding="utf-8", newline="\n") as h:
        h.write("{}\n")                     # 这一版有、下一版删掉的"程序文件"（测双向回滚）
    os.makedirs(os.path.join(target, "app", "data"), exist_ok=True)
    with io.open(os.path.join(target, "app", "data", "applications.db"), "wb") as h:
        h.write(b"USERDATA")
    with io.open(os.path.join(target, "config.json"), "w", encoding="utf-8", newline="\n") as h:
        h.write('{"port": 8768}\n')
    return target


def selftest():
    import tempfile
    ok = True
    results = []

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        results.append((label, cond, extra))
        print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", label, ("  " + extra) if extra else ""))

    root = tempfile.mkdtemp(prefix="updater-selftest-")
    try:
        # ① 正常升级：程序文件换了、用户文件没动、旧文件进了备份
        target = _make_install(root)
        payload = _make_payload(root, removed=["app/data.json"])
        before_user = sha256_file(os.path.join(target, "app", "data", "applications.db"))
        code = run(target, payload)
        check("正常升级：返回 0", code == 0, "code=%s" % code)
        check("正常升级：程序文件已换新",
              "new" in io.open(os.path.join(target, "app", "launcher.py"), encoding="utf-8").read())
        check("正常升级：清单里 removed 的文件真被删了",
              not os.path.isfile(os.path.join(target, "app", "data.json")))
        check("正常升级：用户数据一个字节没变",
              sha256_file(os.path.join(target, "app", "data", "applications.db")) == before_user)
        check("正常升级：有备份目录",
              any(n.startswith("_before_update_") for n in os.listdir(target)))
        check("正常升级：事务记录已清掉",
              not os.path.isfile(os.path.join(txn_dir(target), TXN_NAME)))

        # ② 哈希不符 → 必须回滚（把包里文件改成别的字节，但清单留着旧哈希）
        shutil.rmtree(root, ignore_errors=True)
        root = tempfile.mkdtemp(prefix="updater-selftest-")
        target = _make_install(root)
        payload = _make_payload(root)
        with io.open(os.path.join(payload, "app", "launcher.py"), "w", encoding="utf-8", newline="\n") as h:
            h.write("print('tampered')\n")
        before = io.open(os.path.join(target, "app", "launcher.py"), encoding="utf-8").read()
        code = run(target, payload)
        after = io.open(os.path.join(target, "app", "launcher.py"), encoding="utf-8").read()
        check("哈希不符：返回非 0", code != 0, "code=%s" % code)
        check("哈希不符：已回滚到升级前", before == after)

        # ③ 清单里出现用户文件 → 拒绝
        shutil.rmtree(root, ignore_errors=True)
        root = tempfile.mkdtemp(prefix="updater-selftest-")
        target = _make_install(root)
        payload = _make_payload(root)
        man = load_json(os.path.join(payload, MANIFEST_NAME))
        with io.open(os.path.join(payload, "app", "x.txt"), "w", encoding="utf-8") as h:
            h.write("x")
        man["files"]["app/data/applications.db"] = sha256_file(os.path.join(payload, "app", "x.txt"))
        save_json(os.path.join(payload, MANIFEST_NAME), man)
        try:
            load_manifest(payload)
            check("清单里有用户文件 → 必须拒绝", False)
        except UpdaterError as err:
            check("清单里有用户文件 → 必须拒绝", "用户文件" in err.message, err.message)

        # ④ 越界路径 → 拒绝
        for bad in ("../evil.py", "C:/windows/system32/x.py", "app/../../x.py"):
            man = load_json(os.path.join(payload, MANIFEST_NAME))
            man["files"][bad] = "0" * 64
            save_json(os.path.join(payload, MANIFEST_NAME), man)
            try:
                load_manifest(payload)
                check("越界路径必须拒绝：%s" % bad, False)
            except UpdaterError:
                check("越界路径必须拒绝：%s" % bad, True)

        # ⑤ 白名单之外（包根的可执行文件）→ 拒绝
        man = load_json(os.path.join(payload, MANIFEST_NAME))
        man["files"] = {"evil.exe": "0" * 64}
        save_json(os.path.join(payload, MANIFEST_NAME), man)
        try:
            load_manifest(payload)
            check("白名单之外的路径必须拒绝", False)
        except UpdaterError as err:
            check("白名单之外的路径必须拒绝", "白名单" in err.message, err.message)

        # ⑥ 缺文件 → 拒绝（不进入替换）
        payload = _make_payload(root)
        os.remove(os.path.join(payload, "app", "server.py"))
        target = _make_install(root)
        before = io.open(os.path.join(target, "app", "server.py"), encoding="utf-8").read()
        code = run(target, payload)
        check("包里缺文件：返回非 0", code != 0, "code=%s" % code)
        check("包里缺文件：没动过目标",
              before == io.open(os.path.join(target, "app", "server.py"), encoding="utf-8").read())

        # ⑦ 版本倒退 → 拒绝
        payload = _make_payload(root, version="0.9.0")
        target = _make_install(root, version="1.5.0")
        code = run(target, payload)
        check("升级包比已装版本旧：拒绝", code != 0, "code=%s" % code)

        # ⑧ 目标不像程序目录 → 拒绝
        empty = os.path.join(root, "empty")
        os.makedirs(empty, exist_ok=True)
        code = run(empty, _make_payload(root))
        check("目标不像程序目录：拒绝", code != 0, "code=%s" % code)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description="就地升级（事务化、只碰程序文件）")
    parser.add_argument("--payload", help="解开的升级包目录（里面有 %s）" % MANIFEST_NAME)
    parser.add_argument("--target", help="安装目录（默认：本脚本所在包根）")
    parser.add_argument("--dry-run", action="store_true", help="只演练，不落盘")
    parser.add_argument("--wait", type=int, default=EXIT_WAIT_SECONDS,
                        help="等程序退出最多多少秒（默认 %d；给自检用）" % EXIT_WAIT_SECONDS)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if not args.payload:
        parser.error("要给 --payload（升级包目录）或 --selftest")
    target = args.target or ""
    if target:
        target = os.path.abspath(target)
    else:
        # 没给目标：先自己找，找不到就问（和旧脚本一样的体验，但后面那套事务/白名单/回滚全在）
        say("  没指定旧程序目录，先自动找（桌面/下载/文档，往下三层）……")
        hits = find_installs()
        if len(hits) == 1:
            target = hits[0]
            say("  找到：%s" % target)
        elif len(hits) > 1:
            say("  找到不止一个，请输入编号：")
            for i, item in enumerate(hits):
                say("    %d) %s" % (i + 1, item))
            pick = (input("  编号：") or "").strip()
            if not pick.isdigit() or not (1 <= int(pick) <= len(hits)):
                say("  [X] 输入不对，什么都没改。")
                return 1
            target = hits[int(pick) - 1]
        else:
            say("  没自动找到。把旧程序文件夹拖进本窗口再回车（或留空退出）：")
            typed = (input("  程序目录：") or "").strip().strip('"')
            if not typed:
                say("  [X] 没给目录，什么都没改。")
                return 1
            target = os.path.abspath(typed)
    payload = args.payload
    if not os.path.isdir(payload):
        say("[X] 找不到升级包目录：%s" % payload)
        return 1
    # `run()` 里前置检查抛的错也要**说人话、干净退出**（不许把 traceback 甩给用户）
    try:
        return run(target, payload, args.dry_run, exit_wait=args.wait)
    except UpdaterError as err:
        say("")
        say("   [X] 这次不升级（阶段：%s）：%s" % (err.stage, err.message))
        say("       （什么都没动：程序文件和数据都没被碰）")
        return 1


if __name__ == "__main__":
    sys.exit(main())
