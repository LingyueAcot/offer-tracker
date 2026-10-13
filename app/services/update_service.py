"""更新编排；所有运行时写入都限定在更新缓存，导入本模块没有副作用。

接线：configure(APP_VERSION, paths.program_dir())，启动后 maybe_auto_check()。
notify 接收一个不含这台电脑路径的事件字典；spawn 接收 subprocess.Popen 参数。
"""

import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import uuid
from datetime import datetime
from urllib.parse import urlsplit

import paths
from core import update_rules as rules
from services import github_releases as github
from services import update_store as store


_guard = threading.Lock()
_operation = threading.Lock()
_config = None
_release = None
_DAY = 86400
_MANUAL_INTERVAL = 60
_SUMS_LIMIT = 32 * 1024
_ZIP_LIMIT = 100 * 1024 * 1024
_EXPANDED_LIMIT = 1024 * 1024 * 1024
_BUSY_STAGES = {"checking", "downloading", "installing"}
_INSTALL_TIMEOUT = 600


def configure(app_version, program_dir, updater_path=None, spawn=None, notify=None):
    """注入启动参数；运行中的任务不允许重新配置。"""
    global _config, _release
    version = _version(app_version)
    directory = os.path.realpath(os.fspath(program_dir))
    if not version or not os.path.isdir(directory):
        raise ValueError("更新配置无效")
    with _guard:
        if _operation.locked():
            raise RuntimeError("更新任务正在运行")
        _config = {
            "version": version, "program": directory,
            "updater": os.path.realpath(os.fspath(updater_path)) if updater_path else None,
            "spawn": spawn or subprocess.Popen, "notify": notify,
        }
        _release = None


def _version(value):
    try:
        result = rules.normalize_version(str(value))
    except (TypeError, ValueError):
        return ""
    return result if re.fullmatch(r"[0-9][0-9A-Za-z.+_-]{0,79}", result or "") else ""


def _settings():
    with _guard:
        if _config is None:
            raise RuntimeError("更新功能尚未配置")
        return dict(_config)


def _timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.timestamp()  # 状态日期按本地时间保存，与 update_store 一致。
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _iso(now):
    return datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")


def _url(value):
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username:
            return value
    except ValueError:
        pass
    return ""


def _reply(ok, message="", code=200, **fields):
    return {"ok": ok, "message": message, "http_status": code, **fields}


def _emit(event):
    callback = _settings().get("notify")
    if callback:
        try:
            callback(event)
        except Exception:
            pass  # 通知失败不能改变更新事务的结果。


def _write(**fields):
    state = store.update_state(**fields)
    if not isinstance(state, dict) or any(state.get(k) != v for k, v in fields.items()):
        raise RuntimeError("无法保存更新状态")
    return state


def _failure(message, code=502, check_failure=False, retry_after=None):
    fields = {"stage": "failed", "last_error": message}
    if check_failure:
        state = store.load_state()
        count = max(0, int(state.get("fail_count") or 0)) + 1
        delay = rules.backoff_seconds(count)
        if retry_after is not None:
            try:
                delay = max(delay, min(float(retry_after), 7 * _DAY))
            except (ValueError, TypeError):
                pass
        fields.update(last_check=_iso(time.time()), last_check_ok=False,
                      fail_count=count, next_allowed=_iso(time.time() + delay))
    try:
        _write(**fields)
    except Exception:
        message = "更新失败，且无法保存更新状态，请检查缓存目录是否可写。"
    _emit({"stage": "failed", "message": message})
    return _reply(False, message, code, stage="failed")


def _reserve():
    if not _operation.acquire(blocking=False):
        return False
    try:
        if not store.acquire_lock():
            _operation.release()
            return False
        _recover_install(store.load_state(), _settings())
    except Exception:
        store.release_lock()
        _operation.release()
        raise
    return True


def _finish():
    try:
        store.release_lock()
    finally:
        _operation.release()


def status():
    """只返回公开状态；不泄露 staged_dir、下载文件名或异常原文。"""
    config = _settings()
    state = store.load_state()
    latest = _version(state.get("known_version") or "")
    stage = state.get("stage", "idle")
    recovering = stage == "installing" or (stage == "failed" and state.get("install_token")
                                           and not state.get("install_exit_code"))
    if recovering and _reserve():
        try:
            state = store.load_state()
            stage = state.get("stage", "idle")
        finally:
            _finish()
    if stage not in _BUSY_STAGES | {"idle", "found", "downloaded", "installed", "failed"}:
        stage = "idle"
    # 页面现有契约不识别 installed；持久状态保留完成记录，公开状态映射为 idle。
    if stage == "installed":
        stage = "idle"
    try:
        progress = max(0, min(100, int(state.get("progress") or 0)))
    except (TypeError, ValueError):
        progress = 0
    return {
        "stage": stage, "current": config["version"], "latest": latest,
        "notes": str(state.get("known_body") or ""),
        "url": _url(state.get("known_url")),
        "last_check": _iso(_timestamp(state.get("last_check"))) if state.get("last_check") else "",
        "last_error": _public_error(state.get("last_error"), config),
        "progress": progress,
        "can_install": stage == "downloaded" and _payload_valid(state, config),
        "installed_version": _version(state.get("installed_version") or ""),
    }


def _eligible(state, manual, now):
    allowed = _timestamp(state.get("next_allowed"))
    last = _timestamp(state.get("last_check"))
    if manual:
        allowed = max(allowed if not state.get("last_check_ok") else 0,
                      last + _MANUAL_INTERVAL if last else 0)
        return rules.decide_check(allowed, now, None)[0]
    return rules.decide_check(max(allowed, last + _DAY if last else 0), now, None)[0]


def _check_locked(manual):
    global _release
    config = _settings()
    state = store.load_state()
    if _install_busy(state):
        return _reply(False, "正在安装更新，请稍候。", 409)
    now = time.time()
    if not _eligible(state, manual, now):
        return _reply(False, "检查太频繁，请稍后再试。", 429)
    _write(stage="checking", last_error="")
    result = github.fetch_latest(config["version"], etag=state.get("etag"))
    if not result.get("ok"):
        limited = result.get("rate_limited")
        return _failure("检查失败：服务暂时限流，请稍后重试。" if limited else
                        "检查失败：无法获取版本信息，请检查网络后重试。",
                        429 if limited else 502, True, result.get("retry_after"))
    release = result.get("release")
    if result.get("not_modified"):
        latest = _version(state.get("known_version") or "")
        body = state.get("known_body") or ""
        url = state.get("known_url") or ""
    else:
        if not isinstance(release, dict):
            return _failure("检查失败：版本信息不完整。", check_failure=True)
        latest = _version(release.get("tag_name") or "")
        if not latest or release.get("draft") or release.get("prerelease"):
            return _failure("检查失败：没有有效的正式版本信息。", check_failure=True)
        body, url = str(release.get("body") or ""), _url(release.get("html_url"))
        with _guard:
            _release = dict(release)
    if not latest:
        return _failure("检查失败：缓存的版本信息不完整，请稍后重试。", check_failure=True)
    newer = rules.is_newer(latest, config["version"])
    found = newer and latest != state.get("ignored_version")
    retained = found and state.get("known_version") == latest and _payload_valid(state, config)
    stage = "downloaded" if retained else "found" if found else "idle"
    _write(last_check=_iso(now), last_check_ok=True, fail_count=0,
           next_allowed=_iso(now + _DAY), etag=result.get("etag") or state.get("etag"),
           known_version=latest, known_body=body, known_url=url, stage=stage,
           last_error="", progress=100 if retained else 0,
           downloaded=latest if retained else "", staged_dir=state.get("staged_dir") if retained else "")
    _emit({"stage": stage, "latest": latest})
    return _reply(True, "发现新版本。" if found else "当前已是最新版本或已忽略该版本。",
                  stage=stage, latest=latest)


def check(manual=False):
    try:
        _settings()
        if not _reserve():
            return _reply(False, "已有更新任务正在运行，请稍候。", 409)
    except Exception:
        return _reply(False, "检查失败：更新功能未就绪。", 503)
    try:
        return _check_locked(manual)
    except Exception:
        return _failure("检查失败：无法读取或保存更新信息。", 503, True)
    finally:
        _finish()


def _outside(path, directory):
    try:
        directory = os.path.normcase(os.path.realpath(directory))
        return os.path.commonpath([os.path.normcase(os.path.realpath(path)), directory]) != directory
    except ValueError:
        return True


def _stage_dir(version, config):
    directory = os.path.join(os.path.dirname(store.state_path()), "stage", version)
    if not _outside(directory, config["program"]):
        raise OSError("更新暂存目录位于程序目录内")
    paths.ensure_writable(directory)
    return directory


def _payload_valid(state, config):
    version = _version(state.get("known_version") or "")
    directory = state.get("staged_dir")
    if not state.get("downloaded") or not version or not isinstance(directory, str):
        return False
    if not rules.is_newer(version, config["version"]):
        return False
    candidate = os.path.realpath(directory)
    expected = os.path.realpath(os.path.join(os.path.dirname(store.state_path()),
                                             "stage", version, "payload"))
    return (candidate == expected and _outside(candidate, config["program"])
            and os.path.isfile(os.path.join(candidate, ".update-ready")))


def _clean_download(directory, config):
    """持有任务锁时，只清本版本的下载残留；拒绝链接/联接，避免越界删除。"""
    expected = os.path.realpath(os.path.dirname(store.state_path()))
    if _outside(directory, expected) or not _outside(directory, config["program"]):
        raise OSError("非法更新暂存目录")
    for name in os.listdir(directory):
        item = os.path.join(directory, name)
        info = os.lstat(item)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError("更新暂存内容包含链接")
        if os.path.isdir(item):
            # rmtree 不遍历 Windows 目录联接；仍先拒绝所有已知链接。
            for root, dirs, files in os.walk(item, followlinks=False):
                for child in dirs + files:
                    attrs = os.lstat(os.path.join(root, child))
                    if stat.S_ISLNK(attrs.st_mode) or getattr(attrs, "st_file_attributes", 0) & 0x400:
                        raise OSError("更新暂存内容包含链接")
            shutil.rmtree(item)
        else:
            os.unlink(item)


def _download_locked(version):
    config = _settings()
    with _guard:
        release = dict(_release) if _release else None
    if not release or _version(release.get("tag_name")) != version:
        result = github.fetch_latest(config["version"], etag=None)
        release = result.get("release") if result.get("ok") else None
    if not isinstance(release, dict) or _version(release.get("tag_name")) != version:
        return _failure("下载失败：版本信息已变化，请重新检查更新。")
    archive, sums, reason = rules.select_assets(release, max_size=_ZIP_LIMIT)
    if not archive or not sums:
        return _failure("下载失败：此版本缺少可用的便携包或校验清单。")
    name = archive.get("name", "")
    if (not name or name != os.path.basename(name) or "/" in name or "\\" in name or ":" in name
            or name == rules.CHECKSUM_ASSET_NAME):
        return _failure("下载失败：发布文件名无效。")
    if int(sums.get("size") or 0) > _SUMS_LIMIT:
        return _failure("下载失败：校验清单超过大小限制。")
    directory = _stage_dir(version, config)
    _clean_download(directory, config)
    sums_path = os.path.join(directory, rules.CHECKSUM_ASSET_NAME)
    zip_path = os.path.join(directory, name)

    def limit_manifest(*args, **kwargs):
        size = args[0] if args else kwargs.get("bytes", kwargs.get("downloaded", 0))
        if isinstance(size, dict):
            size = size.get("bytes", size.get("downloaded", 0))
        if int(size or 0) > _SUMS_LIMIT:
            raise ValueError("清单过大")

    result = github.download_asset(_url(sums.get("browser_download_url")), sums_path,
                                   "", on_progress=limit_manifest)
    if not result.get("ok"):
        return _failure("下载失败：无法获取校验清单。")
    if os.path.getsize(sums_path) > _SUMS_LIMIT:
        return _failure("下载失败：校验清单超过大小限制。")
    with io.open(sums_path, "r", encoding="utf-8", newline="\n") as stream:
        checksums = rules.parse_checksum_list(stream.read(_SUMS_LIMIT + 1), limit=_SUMS_LIMIT)
    digest = rules.checksum_for(checksums, name)
    if not digest or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        return _failure("下载失败：校验清单没有有效的安装包校验值。")
    last_progress = [-1]

    def progress(*args, **kwargs):
        size = args[0] if args else kwargs.get("bytes", kwargs.get("downloaded", 0))
        if isinstance(size, dict):
            size = size.get("bytes", size.get("downloaded", 0))
        size = int(size or 0)
        if size > _ZIP_LIMIT:
            raise ValueError("安装包过大")
        percent = min(95, int(size * 95 / max(1, int(archive.get("size") or _ZIP_LIMIT))))
        if percent != last_progress[0]:
            last_progress[0] = percent
            _write(progress=percent)

    result = github.download_asset(_url(archive.get("browser_download_url")), zip_path,
                                   digest, on_progress=progress)
    if not result.get("ok"):
        return _failure("下载失败：安装包下载或校验未通过，请重新下载。")
    if os.path.getsize(zip_path) > _ZIP_LIMIT:
        return _failure("下载失败：安装包超过大小限制。")
    payload = os.path.join(directory, "payload")
    temporary = os.path.join(directory, "payload.tmp")
    if os.path.lexists(temporary) or os.path.lexists(payload):
        return _failure("下载失败：此版本已有暂存内容，请先使用已下载的版本或清理更新缓存。", 409)
    with zipfile.ZipFile(zip_path) as package:
        entries = package.infolist()
        valid, reason = rules.validate_zip_entries([entry.filename for entry in entries])
        if not valid or not entries or sum(entry.file_size for entry in entries) > _EXPANDED_LIMIT:
            return _failure("下载失败：安装包内容不安全或解压后过大。")
        destinations = set()
        for entry in entries:
            target = os.path.realpath(os.path.join(temporary, entry.filename.replace("\\", "/")))
            if (_outside(target, os.path.realpath(temporary))
                    or stat.S_ISLNK(entry.external_attr >> 16)
                    or any(":" in part or part.endswith((".", " "))
                           for part in entry.filename.replace("\\", "/").split("/") if part)):
                return _failure("下载失败：安装包包含非法路径或链接。")
            key = os.path.normcase(target)
            if key in destinations:
                return _failure("下载失败：安装包包含重复路径。")
            destinations.add(key)
        os.makedirs(temporary)
        try:
            package.extractall(temporary)
            marker = os.path.join(temporary, ".update-ready")
            with io.open(marker + ".tmp", "w", encoding="utf-8", newline="\n") as stream:
                stream.write(version + "\n" + digest + "\n")
            os.replace(marker + ".tmp", marker)
            os.replace(temporary, payload)
        except Exception:
            shutil.rmtree(temporary)
            raise
    _write(stage="downloaded", staged_dir=payload, downloaded=version, progress=100, last_error="")
    _emit({"stage": "downloaded", "latest": version})
    return _reply(True, "更新已下载，可以安装。", stage="downloaded")


def _background(action, failure_message):
    try:
        action()
    except Exception:
        _failure(failure_message, 503)
    finally:
        _finish()


def start_download():
    try:
        config = _settings()
        if not _reserve():
            return _reply(False, "已有更新任务正在运行，请稍候。", 409)
    except Exception:
        return _reply(False, "下载失败：更新功能未就绪。", 503)
    handed_off = False
    try:
        state = store.load_state()
        version = _version(state.get("known_version") or "")
        if _install_busy(state):
            return _reply(False, "正在安装更新，请稍候。", 409)
        if not version or not rules.is_newer(version, config["version"]):
            return _reply(False, "请先检查更新，确认有可下载的新版本。", 409)
        if version == state.get("ignored_version"):
            return _reply(False, "此版本已忽略，请等待其它新版本。", 409)
        if _payload_valid(state, config):
            return _reply(False, "此版本已下载，请直接安装。", 409, stage="downloaded")
        _write(stage="downloading", downloaded="", staged_dir="", progress=0, last_error="")
        threading.Thread(target=_background,
                         args=(lambda: _download_locked(version), "下载失败：无法保存或解压更新包。"),
                         daemon=True, name="update-download").start()
        handed_off = True
        return _reply(True, stage="downloading")
    except Exception:
        return _failure("下载失败：无法启动下载任务。", 503)
    finally:
        if not handed_off:
            _finish()


def _installer(config):
    parent = os.path.dirname(config["program"])
    updater = config["updater"] or os.path.join(parent, "updater", "updater.py")
    if not _outside(updater, config["program"]) or not os.path.isfile(updater):
        raise FileNotFoundError("程序目录外没有更新器")
    candidates = [os.path.join(parent, "runtime", "pythonw.exe"),
                  os.path.join(parent, "runtime", "python.exe"), sys.executable]
    python = next((item for item in candidates if os.path.isfile(item)), None)
    if not python:
        raise FileNotFoundError("没有可用的 Python 运行时")
    return python, updater


# 复制到安装树之外执行：后端退出、甚至 services 被替换后仍能收集结果。
# 命令行只含外部 job 路径，避免被更新器的安装目录进程检查误认成目标程序。
_MONITOR_SOURCE = r'''
import json, os, subprocess, sys
with open(sys.argv[1], encoding="utf-8") as f:
    job = json.load(f)
result = {"token": job["token"], "code": None, "timeout": False, "output": ""}
try:
    with open(job["log"], "w+b") as log:
        child = subprocess.Popen(job["command"], cwd=os.path.dirname(job["command"][1]),
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                 env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        try:
            result["code"] = child.wait(timeout=job["timeout"])
        except subprocess.TimeoutExpired:
            result["timeout"] = True
            child.kill()
            result["code"] = child.wait(timeout=10)
        log.seek(0, 2)
        log.seek(max(0, log.tell() - 16384))
        result["output"] = log.read().decode("utf-8", "replace")
except Exception as exc:
    result["output"] = str(exc)
with open(job["result"] + ".tmp", "w", encoding="utf-8", newline="\n") as f:
    json.dump(result, f, ensure_ascii=False)
    f.flush()
    os.fsync(f.fileno())
os.replace(job["result"] + ".tmp", job["result"])
'''


def _monitor_dir(token):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise ValueError("安装任务标识无效")
    return os.path.realpath(os.path.join(tempfile.gettempdir(), "ResumeTrackerInstall", token))


def _public_error(value, config):
    message = str(value or "")
    # 自己产生的错误可显示；更新器的原始输出只保存在缓存里。
    for path in (config["program"], os.path.dirname(config["program"]),
                 os.path.dirname(store.state_path()), tempfile.gettempdir()):
        message = message.replace(path, "[这台电脑路径]")
    return message[:500]


def _recover_install(state, config):
    if state.get("stage") not in {"installing", "failed"}:
        return
    if state.get("stage") == "failed" and (not state.get("install_token") or state.get("install_exit_code")):
        return
    token = state.get("install_token")
    try:
        receipt = os.path.join(_monitor_dir(token), "result.json")
        with io.open(receipt, encoding="utf-8") as stream:
            result = json.loads(stream.read(65536))
        if not isinstance(result, dict):
            raise ValueError("安装结果格式无效")
        if result.get("token") != token or not isinstance(result.get("timeout"), bool):
            raise ValueError("安装结果不匹配")
        code = result.get("code")
        if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
            raise ValueError("安装退出码无效")
        success = code == 0 and not result["timeout"]
        error = ("" if success else "安装失败：更新器超时，可能存在未完成事务，请重试更新器恢复。"
                 if result["timeout"] else "安装失败：更新器退出码 %s；详细输出已保存在更新缓存。" % code)
        _write(stage="installed" if success else "failed", last_error=error,
               installed_version=state.get("known_version") if success else "",
               install_exit_code=str(code), install_output=str(result.get("output") or "")[-16384:],
               install_pid="", install_started="",
               progress=100 if success else state.get("progress", 0))
        return
    except (OSError, ValueError, TypeError):
        pass
    # FILETIME 防止 PID 复用；读取不了进程时在有限窗口内保守等待。
    try:
        pid = int(state.get("install_pid") or 0)
    except ValueError:
        pid = 0
    alive, started = store._process_info(pid) if pid else ("dead", None)
    age = time.time() - _timestamp(state.get("install_time"))
    if alive == "alive" and started == state.get("install_started"):
        return
    if (not pid and age < 30) or (alive == "unknown" and age < _INSTALL_TIMEOUT + 30):
        return
    phase = _transaction_phase(config)
    _write(stage="failed", last_error="安装中断：未取得可靠的完成结果，可能有未完成事务%s；请重试更新器恢复。" % phase,
           install_exit_code="interrupted", install_pid=state.get("install_pid", "") if alive == "unknown" else "",
           install_started=state.get("install_started", "") if alive == "unknown" else "")


def _install_busy(state):
    if state.get("stage") == "installing":
        return True
    if state.get("stage") != "failed" or not state.get("install_pid"):
        return False
    try:
        alive, started = store._process_info(int(state["install_pid"]))
    except ValueError:
        return False
    return alive == "unknown" or (alive == "alive" and started == state.get("install_started"))


def _transaction_phase(config):
    """只读事务现场；不在服务层尝试修改/回滚程序文件。"""
    candidates = [os.path.join(os.path.dirname(config["program"]), "更新数据", "update-txn.json"),
                  os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                               "ResumeTracker", "cache", "update-txn.json")]
    for filename in candidates:
        try:
            with io.open(filename, encoding="utf-8") as stream:
                phase = json.loads(stream.read(65536)).get("phase")
            if phase in {"preflight", "wait-exit", "backup", "apply", "verify", "done", "rollback", "rollback-failed"}:
                return "（事务阶段：%s）" % phase
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return ""


def _prepare_monitor(config, state, python, updater, token):
    directory = _monitor_dir(token)
    root = os.path.dirname(config["program"])
    if not _outside(directory, root):
        raise OSError("安装监控必须在安装目录之外")
    os.makedirs(directory, exist_ok=False)
    # 便携 Python 在安装树内，会被退出等待发现；复制到本次外部缓存运行。
    if not _outside(python, root):
        runtime = os.path.join(directory, "runtime")
        shutil.copytree(os.path.dirname(python), runtime)
        python = os.path.join(runtime, os.path.basename(python))
    copied_updater = os.path.join(directory, "updater.py")
    shutil.copyfile(updater, copied_updater)
    payload = state["staged_dir"]
    # 状态目录最后的便携兜底在安装根里；更新器不允许从目标树内安装负载。
    if not _outside(payload, root):
        external_payload = os.path.join(directory, "payload")
        shutil.copytree(payload, external_payload)
        payload = external_payload
    command = [python, copied_updater, "--payload", payload, "--target", root]
    job = {"token": token, "command": command, "timeout": _INSTALL_TIMEOUT,
           "result": os.path.join(directory, "result.json"), "log": os.path.join(directory, "installer.log")}
    script = os.path.join(directory, "monitor.py")
    job_path = os.path.join(directory, "job.json")
    with io.open(script, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(_MONITOR_SOURCE)
    with io.open(job_path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(job, stream, ensure_ascii=False)
    return [python, script, job_path]


def install():
    try:
        config = _settings()
        if not _reserve():
            return _reply(False, "已有更新任务正在运行，请稍候。", 409)
    except Exception:
        return _reply(False, "安装失败：更新功能未就绪。", 503)
    handed_off = False
    try:
        state = store.load_state()
        if _install_busy(state):
            return _reply(False, "正在安装更新，请稍候。", 409)
        if state.get("stage") != "downloaded" or not _payload_valid(state, config):
            return _reply(False, "请先完整下载更新，再安装。", 409)
        if state.get("known_version") == state.get("ignored_version"):
            return _reply(False, "此版本已忽略。", 409)
        try:
            python, updater = _installer(config)
        except FileNotFoundError:
            return _reply(False, "安装失败：找不到程序目录外的更新器或可用运行时，请重新获取完整便携包。", 503)
        token = uuid.uuid4().hex
        command = _prepare_monitor(config, state, python, updater, token)
        _write(stage="installing", last_error="", install_token=token,
               install_pid="", install_started="", install_time=_iso(time.time()),
               install_output="", install_exit_code="", installed_version="")

        # 先真起监控进程，再允许前端关窗。仅回复 installing，不宣称安装成功。
        child = config["spawn"](command, cwd=os.path.dirname(command[1]),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _, started = store._process_info(child.pid)
        _write(install_pid=str(child.pid), install_started=started or "")

        def launch():
            _emit({"stage": "installing", "close_window": True})
            try:
                output, errors = child.communicate(timeout=_INSTALL_TIMEOUT + 30)
            except subprocess.TimeoutExpired:
                return _failure("安装失败：监控进程超时，请检查未完成事务后重试。", 504)
            if child.returncode != 0:
                _write(install_exit_code=str(child.returncode),
                       install_output=((output or b"") + (errors or b"")).decode("utf-8", "replace")[-16384:])
                return _failure("安装失败：监控进程退出码 %s。" % child.returncode)
            _recover_install(store.load_state(), config)
            if store.load_state().get("stage") == "installing":
                return _failure("安装失败：监控结束但没有可靠的完成结果。")
            _emit({"stage": store.load_state().get("stage")})

        threading.Thread(target=_background,
                         args=(launch, "安装失败：无法启动更新器，请重新下载或重试。"),
                         daemon=True, name="update-install").start()
        handed_off = True
        return _reply(True, "即将启动更新器，请关闭窗口。", stage="installing", close_window=True)
    except Exception:
        return _failure("安装失败：无法准备安装任务。", 503)
    finally:
        if not handed_off:
            _finish()


def ignore(version):
    try:
        _settings()
        value = _version(version)
        if not value:
            return _reply(False, "请提供有效的版本号。", 400)
        if not _reserve():
            return _reply(False, "已有更新任务正在运行，请稍候。", 409)
    except Exception:
        return _reply(False, "忽略失败：更新功能未就绪。", 503)
    try:
        state = store.load_state()
        if _install_busy(state):
            return _reply(False, "正在安装更新，请稍候。", 409)
        if value != _version(state.get("known_version") or ""):
            return _reply(False, "只能忽略当前检查到的版本，请重新检查更新。", 409)
        _write(ignored_version=value, stage="idle", last_error="")
        return _reply(True, "已忽略此版本。", stage="idle", latest=value)
    except Exception:
        return _reply(False, "忽略失败：无法保存更新设置。", 503)
    finally:
        _finish()


def maybe_auto_check():
    """在后台判断每日间隔和退避；启动调用无需等待磁盘或网络。"""
    try:
        threading.Thread(target=check, kwargs={"manual": False},
                         daemon=True, name="update-auto-check").start()
        return True
    except RuntimeError:
        return False
