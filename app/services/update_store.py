"""
更新状态的文件服务。

放在 services/，因为这一层解析可写目录、读取和原子写入 JSON，
并使用 Windows 文件锁及进程信息实现跨进程互斥；不依赖 server。

关键取舍：
- 优先使用 paths.cache_dir()/update，失败后使用 paths.data_dir()/update。
  候选目录必须在 paths.program_dir() 之外，否则拒绝使用。
- 成功选定目录后，本进程固定使用该目录，避免状态和锁分别落到不同位置。
- 状态只保留约定字段，保存采用 state.json.tmp 加 os.replace。
- 锁使用持续存在的文件及 Windows 一字节非阻塞锁，并记录 PID 和进程
  创建时间 FILETIME；崩溃后由系统释放文件锁，再核验旧进程记录。
- acquire_lock 不可重入；同进程已持有任务锁时，save_state/update_state
  可以从其他线程安全写状态，不会释放已有任务锁。
- update_state 写入失败或未获得锁时返回当前已保存状态，并记录错误；
  返回的字典不会伪装成已经保存的结果。

需要避开的坑：
- PID 会复用，不能只判断 PID 是否存在，也不能用本次锁的写入时间代替
  进程真正的启动时间。
- 释放锁时不能删除锁文件，否则不同进程可能锁住不同文件对象。
- 权限不足、进程启动时间无法确认时保守判定为仍被占用。
- 缺文件或损坏状态返回空字典；存在且有效的状态会补齐默认字段。
- 如果路径契约的两个候选目录都位于程序目录内，无法同时满足位置约束；
  state_path/lock_path 抛出 RuntimeError，其他公开接口按约定安全失败。
"""

import ctypes
from ctypes import wintypes
import datetime
from contextlib import contextmanager
import io
import json
import math
import os
import sys
import threading

import paths


if os.name == "nt":
    import msvcrt
else:
    msvcrt = None


_DEFAULT_STATE = {
    "last_check": "",
    "last_check_ok": False,
    "fail_count": 0,
    "next_allowed": "",
    "etag": "",
    "known_version": "",
    "known_body": "",
    "known_url": "",
    "ignored_version": "",
    "stage": "idle",
    "staged_dir": "",
    "downloaded": "",
    "progress": 0,
    "last_error": "",
    "install_token": "",
    "install_pid": "",
    "install_started": "",
    "install_time": "",
    "install_output": "",
    "install_exit_code": "",
    "installed_version": "",
}
_STAGES = frozenset({
    "idle", "checking", "found", "downloading",
    "downloaded", "installing", "installed", "failed",
})
_STATE_LIMIT = 2 * 1024 * 1024
_LOCK_LIMIT = 4096

_mutex = threading.RLock()
_storage_dir = None
_held_stream = None
_held_pid = None
_owner_thread = None
_kernel32 = None


def _log(message):
    try:
        sys.stderr.write(f"[更新状态] {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _inside(candidate, parent):
    candidate = os.path.normcase(os.path.realpath(candidate))
    parent = os.path.normcase(os.path.realpath(parent))
    try:
        return os.path.commonpath([candidate, parent]) == parent
    except ValueError:
        # 不同盘符不会构成父子目录。
        return False


def _resolve_storage_dir():
    global _storage_dir
    if _storage_dir is not None:
        return _storage_dir

    program = os.path.abspath(os.fspath(paths.program_dir()))
    errors = []

    def _local_appdata_candidate():
        # ⚠️ 一定要有一条"**无论哪种装法都在程序目录之外**"的候选：
        #    便携包与原地装法的 cache_dir()/data_dir() 都在程序目录里（相邻布局），
        #    只靠那两个候选会让"取更新锁"必然失败 —— 2026-10-12 打包后首启实测到的：
        #    `[更新状态] 取得更新锁失败：无法创建程序目录之外的更新状态目录`。
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "ResumeTracker")

    def _package_root_candidate():
        # 便携包：`<包>\更新数据`（在 app\ 之外、跟着包走，U 盘拔了状态也在）
        return os.path.join(os.path.dirname(program), "更新数据")

    for provider in (paths.cache_dir, paths.data_dir, _local_appdata_candidate,
                     _package_root_candidate):
        try:
            base = os.path.abspath(os.fspath(provider()))
            candidate = os.path.realpath(os.path.join(base, "update"))
            if _inside(candidate, program):
                errors.append("候选更新目录位于程序目录内")
                continue

            paths.ensure_writable(candidate)
            # 建目录后再次解析，防止已有目录联接指向程序目录。
            resolved = os.path.realpath(candidate)
            if _inside(resolved, program):
                errors.append("候选更新目录联接到了程序目录内")
                continue

            _storage_dir = resolved
            return resolved
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(str(exc))

    detail = "；".join(errors) or "没有可用的候选目录"
    raise RuntimeError(f"无法创建程序目录之外的更新状态目录：{detail}")


def state_path() -> str:
    """返回外部更新目录中的 state.json；无合规可写目录时抛 RuntimeError。"""
    with _mutex:
        return os.path.join(_resolve_storage_dir(), "state.json")


def lock_path() -> str:
    """返回与状态同目录的持久锁文件路径。"""
    with _mutex:
        return os.path.join(_resolve_storage_dir(), "update.lock")


def _reject_json_constant(value):
    raise ValueError(f"JSON 中包含非法数值：{value}")


def _canonical_state(data):
    if not isinstance(data, dict):
        raise ValueError("更新状态必须是字典")

    result = _DEFAULT_STATE.copy()
    for key, default in _DEFAULT_STATE.items():
        if key not in data:
            continue
        value = data[key]

        if key == "last_check_ok":
            if not isinstance(value, bool):
                raise ValueError("last_check_ok 必须是布尔值")
        elif key == "fail_count":
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError("fail_count 必须是非负整数")
        elif key == "progress":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
            ):
                raise ValueError("progress 必须是 0 到 100 的数字")
            try:
                valid = math.isfinite(value) and 0 <= value <= 100
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("progress 必须是 0 到 100 的数字")
        else:
            # 网络层的空 ETag 等可直接清空对应字符串字段。
            if value is None:
                value = default
            if not isinstance(value, str):
                raise ValueError(f"{key} 必须是字符串")
            if key == "stage" and value not in _STAGES:
                raise ValueError("更新阶段无效")
            if key in {"last_check", "next_allowed"} and value:
                try:
                    parsed = datetime.datetime.strptime(
                        value, "%Y-%m-%d %H:%M:%S"
                    )
                except ValueError as exc:
                    raise ValueError(f"{key} 日期格式无效") from exc
                if parsed.strftime("%Y-%m-%d %H:%M:%S") != value:
                    raise ValueError(f"{key} 日期格式无效")

        result[key] = value
    return result


def _load_unlocked():
    try:
        with io.open(
            state_path(), "r", encoding="utf-8", newline="\n"
        ) as stream:
            text = stream.read(_STATE_LIMIT + 1)
        if (
            len(text) > _STATE_LIMIT
            or len(text.encode("utf-8")) > _STATE_LIMIT
        ):
            raise ValueError("更新状态文件超过大小上限")
        data = json.loads(
            text, parse_constant=_reject_json_constant
        )
        return _canonical_state(data)
    except FileNotFoundError:
        return {}
    except (
        OSError, RuntimeError, TypeError, ValueError,
        UnicodeError, RecursionError,
    ) as exc:
        _log(f"读取更新状态失败：{exc}")
        return {}


def load_state() -> dict:
    """缺文件、坏 JSON 或无效状态返回空字典；读取不占用任务锁。"""
    return _load_unlocked()


def _windows_api():
    global _kernel32
    if _kernel32 is not None:
        return _kernel32
    if os.name != "nt":
        raise RuntimeError("更新跨进程锁仅支持 Windows")

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
    ]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    api.GetProcessTimes.restype = wintypes.BOOL
    api.WaitForSingleObject.argtypes = [
        wintypes.HANDLE, wintypes.DWORD
    ]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    _kernel32 = api
    return api


def _process_info(pid):
    """返回 (alive/dead/unknown, 创建时间字符串或 None)。"""
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or not 0 < pid <= 0xFFFFFFFF
    ):
        return "unknown", None

    try:
        api = _windows_api()
    except (OSError, RuntimeError, AttributeError):
        return "unknown", None

    # 同时需要查询创建时间和等待权限，避免把退出码 259 误认成仍运行。
    handle = api.OpenProcess(0x1000 | 0x00100000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        return ("dead", None) if error == 87 else ("unknown", None)

    try:
        wait_result = api.WaitForSingleObject(handle, 0)
        if wait_result == 0:
            return "dead", None
        if wait_result != 0x102:
            return "unknown", None

        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not api.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return "unknown", None

        token = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return "alive", str(token)
    finally:
        api.CloseHandle(handle)


def _read_lock_metadata(stream):
    stream.seek(1)
    text = stream.read(_LOCK_LIMIT + 1)
    if not text.strip() or len(text) > _LOCK_LIMIT:
        return {}
    try:
        data = json.loads(
            text, parse_constant=_reject_json_constant
        )
        return data if isinstance(data, dict) else {}
    except (ValueError, RecursionError):
        return {}


def _write_lock_metadata(stream, data):
    # 首字节长期保留，原生锁始终锁同一字节；a+ 在截断后的尾部追加。
    stream.seek(1)
    stream.truncate()
    stream.write(json.dumps(data, ensure_ascii=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def _recorded_owner_alive(metadata):
    pid = metadata.get("pid")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or not 0 < pid <= 0xFFFFFFFF
    ):
        return False

    status, actual_started = _process_info(pid)
    if status == "dead":
        return False
    if status == "unknown":
        return True

    recorded_started = metadata.get("started")
    if (
        not isinstance(recorded_started, str)
        or not recorded_started.isascii()
        or not recorded_started.isdigit()
    ):
        # 旧记录缺少可靠启动时间时，不冒险抢一个仍活着的 PID。
        return True
    return recorded_started == actual_started


def _acquire_lock_locked():
    global _held_stream, _held_pid, _owner_thread
    if _held_stream is not None:
        return False
    if msvcrt is None:
        _log("更新跨进程锁仅支持 Windows")
        return False

    stream = None
    native_locked = False
    retained = False
    try:
        stream = io.open(
            lock_path(), "a+", encoding="utf-8", newline="\n"
        )
        stream.seek(0)
        try:
            # Windows 允许锁住 EOF 之后的字节，空锁文件也可安全初始化。
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        native_locked = True

        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write("\n")
            stream.flush()

        metadata = _read_lock_metadata(stream)
        if _recorded_owner_alive(metadata):
            return False

        pid = os.getpid()
        status, started = _process_info(pid)
        if status != "alive" or started is None:
            _log("无法确认当前进程启动时间，未取得更新锁")
            return False

        _write_lock_metadata(stream, {
            "pid": pid,
            "started": started,
        })
        _held_stream = stream
        _held_pid = pid
        _owner_thread = threading.get_ident()
        retained = True
        return True
    except (
        OSError, RuntimeError, TypeError, ValueError,
        UnicodeError, AttributeError,
    ) as exc:
        _log(f"取得更新锁失败：{exc}")
        return False
    finally:
        if stream is not None and not retained:
            if native_locked:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, ValueError):
                    pass
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def acquire_lock() -> bool:
    """非阻塞取得任务锁；已有锁或旧记录对应活进程时返回 False。"""
    with _mutex:
        return _acquire_lock_locked()


def _release_lock_locked():
    global _held_stream, _held_pid, _owner_thread
    stream = _held_stream
    if stream is None:
        return

    try:
        _write_lock_metadata(stream, {})
    except (OSError, ValueError, UnicodeError) as exc:
        _log(f"清理更新锁记录失败：{exc}")
    finally:
        try:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        except (OSError, ValueError, AttributeError) as exc:
            _log(f"释放更新文件锁失败：{exc}")
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
            _held_stream = None
            _held_pid = None
            _owner_thread = None


def release_lock() -> None:
    """释放本进程任务锁；允许请求线程把任务交给后台线程后由后台收尾。"""
    with _mutex:
        if _held_stream is None:
            return
        if _held_pid != os.getpid():
            _log("当前进程不是任务锁持有者，未释放更新锁")
            return
        _release_lock_locked()


@contextmanager
def _state_guard():
    # RLock 同时保护本进程的读改写和任务锁生命周期。
    with _mutex:
        acquired_here = False
        if _held_stream is not None:
            ready = _held_pid == os.getpid()
        else:
            ready = _acquire_lock_locked()
            acquired_here = ready
        try:
            yield ready
        finally:
            if acquired_here:
                _release_lock_locked()


def _save_unlocked(data):
    temporary = None
    owns_temporary = False
    try:
        canonical = _canonical_state(data)
        text = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        ) + "\n"
        if len(text.encode("utf-8")) > _STATE_LIMIT:
            raise ValueError("更新状态超过大小上限")

        destination = state_path()
        temporary = destination + ".tmp"

        # 只有持有跨进程锁后才清理上次崩溃留下的固定临时文件。
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

        with io.open(
            temporary, "x", encoding="utf-8", newline="\n"
        ) as stream:
            owns_temporary = True
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary, destination)
        owns_temporary = False
        return True
    except (
        OSError, RuntimeError, TypeError, ValueError,
        UnicodeError, OverflowError, RecursionError,
    ) as exc:
        _log(f"保存更新状态失败：{exc}")
        return False
    finally:
        if owns_temporary and temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                _log("无法删除更新状态临时文件")


def save_state(data: dict) -> bool:
    """取得写入锁并原子保存固定结构；失败返回 False，不抛出普通异常。"""
    try:
        with _state_guard() as ready:
            if not ready:
                _log("更新状态被其他进程占用，未保存")
                return False
            return _save_unlocked(data)
    except Exception as exc:
        _log(f"保存更新状态失败：{exc}")
        return False


def update_state(**fields) -> dict:
    """
    在锁内读取、修改并保存，返回实际保存的完整状态。

    不认识的字段、无效值或写入失败不会改变已有状态。
    """
    try:
        with _state_guard() as ready:
            if not ready:
                _log("更新状态被其他进程占用，未修改")
                return _load_unlocked()

            current = _load_unlocked()
            unknown = set(fields) - set(_DEFAULT_STATE)
            if unknown:
                _log("更新状态包含未约定的字段，未修改")
                return current

            candidate = _DEFAULT_STATE.copy()
            candidate.update(current)
            candidate.update(fields)
            try:
                candidate = _canonical_state(candidate)
            except (TypeError, ValueError, OverflowError) as exc:
                _log(f"更新状态字段无效：{exc}")
                return current

            if _save_unlocked(candidate):
                return candidate
            return current
    except Exception as exc:
        _log(f"修改更新状态失败：{exc}")
        return _load_unlocked()
