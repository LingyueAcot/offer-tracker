"""
GitHub 发布查询与附件下载服务。

放在 services/，因为这一层执行 HTTPS 请求、读取时钟和写下载文件；
版本和附件选择交给 core，不依赖 server，也不在这里启动后台线程。

关键取舍：
- 使用系统默认 TLS 校验，限制 HTTPS 来源和重定向目标。
- 单次调用不自动重试、不睡眠；返回 retry_after 秒数，由调用方落实限流。
- 查询响应最多 1 MiB，更新包最多 100 MiB，校验清单最多 32 KiB。
- 下载先写 .part，校验通过后 os.replace；失败只删除本次创建的临时文件。
- expected_sha256 为空时，只允许下载本项目的校验清单，不能下载 ZIP。
- 进度回调采用 on_progress(已下载字节数, 总字节数或 None)。

需要避开的坑：
- urllib 把 304、403、429 等状态包装为 HTTPError，必须单独分类。
- 普通 403 不能仅凭存在 X-RateLimit-Reset 就判定限流。
- 二进制 ZIP 不能做 UTF-8 编码转换；按统一开文件契约使用 io.open，
  实际把原始字节写入其 buffer。
- timeout 是单次网络等待上限，budget 在连接、重定向和每次读取前后检查；
  同步标准库无法强制取消正在进行的系统 DNS 解析。
- 304 不携带可用发布正文，调用方必须保留原来的发布缓存。
"""

import datetime
import email.utils
import hashlib
import hmac
import io
import json
import math
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException

import paths
from core.update_rules import (
    ASSET_ZIP_PREFIX,
    CHECKSUM_ASSET_NAME,
    normalize_version,
)


API_LATEST = (
    "https://api.github.com/repos/"
    "LingyueAcot/offer-tracker/releases/latest"
)

_API_LIMIT = 1024 * 1024
_DOWNLOAD_LIMIT = 100 * 1024 * 1024
_CHECKSUM_LIMIT = 32 * 1024
_CHUNK_SIZE = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DOWNLOAD_HOSTS = frozenset({
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
})
_RELEASE_PATH = "/LingyueAcot/offer-tracker/releases/download/"


class _BudgetExpired(TimeoutError):
    pass


def _log(message):
    try:
        sys.stderr.write(f"[更新网络] {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _positive_seconds(value):
    if isinstance(value, bool):
        raise ValueError("时间参数不能是布尔值")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("时间参数必须是有限正数")
    return result


def _remaining(deadline, timeout):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _BudgetExpired("已超过总时间预算")
    return min(timeout, remaining)


def _safe_etag(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    try:
        value.encode("latin-1")
    except UnicodeError:
        return None
    return value


def _check_url(url, download, initial=False):
    if (
        not isinstance(url, str)
        or len(url) > 16384
        or any(ord(char) <= 32 or ord(char) == 127 for char in url)
        or "\\" in url
    ):
        raise ValueError("下载或请求地址格式无效")

    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("下载或请求地址格式无效") from exc

    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise ValueError("更新请求只允许不带账户信息的 HTTPS 地址")

    host = (parsed.hostname or "").lower()
    if not download:
        if host != "api.github.com":
            raise ValueError("发布查询不能跳转到 GitHub API 以外")
    elif host not in _DOWNLOAD_HOSTS:
        raise ValueError("附件不能跳转到未经允许的下载主机")

    if download and initial:
        if (
            host != "github.com"
            or not parsed.path.startswith(_RELEASE_PATH)
        ):
            raise ValueError("下载地址必须是本项目的 GitHub 发布附件")
        tail = parsed.path[len(_RELEASE_PATH):].split("/")
        if len(tail) != 2 or not all(tail):
            raise ValueError("发布附件地址缺少版本或文件名")
        decoded = [urllib.parse.unquote(item) for item in tail]
        if any(
            item in {".", ".."} or "/" in item or "\\" in item
            for item in decoded
        ):
            raise ValueError("发布附件地址含有无效路径")
    return parsed


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, download, deadline, timeout):
        super().__init__()
        self.download = download
        self.deadline = deadline
        self.timeout = timeout

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _remaining(self.deadline, self.timeout)
        _check_url(newurl, self.download)
        redirected = super().redirect_request(
            req, fp, code, msg, headers, newurl
        )
        # 下载请求不会携带 API ETag；重定向也不添加认证信息。
        return redirected

    def http_error_302(self, req, fp, code, msg, headers):
        req.timeout = _remaining(self.deadline, self.timeout)
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def _opener(download, deadline, timeout):
    return urllib.request.build_opener(
        _SafeRedirectHandler(download, deadline, timeout)
    )


def _headers(version):
    version = normalize_version(version) or "unknown"
    return {
        "User-Agent": (
            f"offer-tracker/{version} (LingyueAcot/offer-tracker)"
        ),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Accept-Encoding": "identity",
    }


def _retry_information(status, headers):
    """返回相对等待秒数与是否限流；不把时间戳直接交给调用方。"""
    remaining = str(headers.get("X-RateLimit-Remaining", "")).strip()
    retry_value = str(headers.get("Retry-After", "")).strip()
    limited = (
        status == 429
        or (
            status == 403
            and (remaining == "0" or bool(retry_value))
        )
    )

    now = time.time()
    delays = []
    if retry_value:
        try:
            seconds = float(retry_value)
            if math.isfinite(seconds) and seconds >= 0:
                delays.append(seconds)
        except (TypeError, ValueError, OverflowError):
            try:
                retry_date = email.utils.parsedate_to_datetime(retry_value)
                if retry_date.tzinfo is None:
                    retry_date = retry_date.replace(
                        tzinfo=datetime.timezone.utc
                    )
                delay = retry_date.timestamp() - now
                if math.isfinite(delay):
                    delays.append(max(0.0, delay))
            except (TypeError, ValueError, OverflowError, OSError):
                pass

    if limited:
        try:
            reset = float(headers.get("X-RateLimit-Reset", ""))
            if math.isfinite(reset) and reset >= 0:
                delays.append(max(0.0, reset - now))
        except (TypeError, ValueError, OverflowError):
            pass

    return (max(delays) if delays else None), limited


def _http_failure(status, headers, download=False):
    retry_after, limited = _retry_information(status, headers)
    if limited:
        reason = "GitHub 请求受到限流，请等待后重试"
    elif status == 403:
        reason = "GitHub 拒绝访问，此响应未表明限流"
    elif status == 404:
        reason = (
            "发布附件不存在或已被移除"
            if download else "没有可访问的正式发布，或仓库不可访问"
        )
    elif 500 <= status <= 599:
        reason = "GitHub 服务暂时不可用"
    elif status == 401:
        reason = "GitHub 请求未获授权"
    else:
        reason = f"GitHub 返回未预期的 HTTP 状态：{status}"
    return {
        "ok": False,
        "status": status,
        "reason": reason,
        "retry_after": retry_after,
        "rate_limited": limited,
    }


def _content_length(headers):
    value = headers.get("Content-Length")
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("服务器返回了无效的文件大小") from exc
    if length < 0:
        raise ValueError("服务器返回了无效的文件大小")
    return length


def _set_read_timeout(response, seconds):
    # urllib 没有公开的逐次读取超时接口；兼容标准 HTTPResponse 的底层套接字。
    try:
        response.fp.raw._sock.settimeout(seconds)
    except (AttributeError, OSError, ValueError):
        pass


def _read_chunk(response, amount, deadline, timeout):
    seconds = _remaining(deadline, timeout)
    _set_read_timeout(response, seconds)
    reader = getattr(response, "read1", None)
    chunk = (
        reader(amount) if callable(reader) else response.read(amount)
    )
    _remaining(deadline, timeout)
    return chunk


def _reject_json_constant(value):
    raise ValueError(f"JSON 中包含非法数值：{value}")


def _network_reason(exc, action):
    if isinstance(exc, _BudgetExpired):
        return f"{action}超过总时间预算"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f"{action}连接或读取超时"
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return f"{action}连接或读取超时"
        return f"{action}失败，网络、代理或 TLS 连接不可用"
    if isinstance(exc, HTTPException):
        return f"{action}响应中断或协议错误"
    if isinstance(exc, ValueError):
        return str(exc)
    return f"{action}失败，网络连接或文件读写不可用"


def fetch_latest(
    current_version, etag=None, timeout=10, budget=30
) -> dict:
    """查询最新正式发布；retry_after 的单位是秒，不会在本函数内重试。"""
    result = {
        "ok": False,
        "status": 0,
        "release": None,
        "etag": None,
        "not_modified": False,
        "reason": "",
        "retry_after": None,
        "rate_limited": False,
    }
    try:
        timeout = _positive_seconds(timeout)
        budget = _positive_seconds(budget)
        deadline = time.monotonic() + budget
        headers = _headers(current_version)
        previous_etag = _safe_etag(etag)
        if previous_etag:
            headers["If-None-Match"] = previous_etag

        _check_url(API_LATEST, download=False)
        request = urllib.request.Request(API_LATEST, headers=headers)
        opener = _opener(False, deadline, timeout)

        with opener.open(
            request, timeout=_remaining(deadline, timeout)
        ) as response:
            result["status"] = response.getcode()
            result["etag"] = _safe_etag(response.headers.get("ETag"))
            if result["status"] == 304:
                result.update(
                    ok=True,
                    not_modified=True,
                    etag=result["etag"] or previous_etag,
                )
                return result
            if result["status"] != 200:
                result.update(
                    _http_failure(result["status"], response.headers)
                )
                return result

            total = _content_length(response.headers)
            if total is not None and total > _API_LIMIT:
                result["reason"] = "GitHub API 响应超过 1 MiB 上限"
                return result

            body = bytearray()
            while True:
                chunk = _read_chunk(
                    response,
                    min(_CHUNK_SIZE, _API_LIMIT - len(body) + 1),
                    deadline,
                    timeout,
                )
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > _API_LIMIT:
                    result["reason"] = "GitHub API 响应超过 1 MiB 上限"
                    return result
            if total is not None and len(body) != total:
                result["reason"] = "GitHub API 响应未完整接收"
                return result

        try:
            release = json.loads(
                body.decode("utf-8-sig"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, ValueError, RecursionError):
            result["reason"] = "GitHub API 返回的内容不是有效 JSON"
            return result

        if (
            not isinstance(release, dict)
            or not isinstance(release.get("tag_name"), str)
            or not release["tag_name"]
            or not isinstance(release.get("assets"), list)
        ):
            result["reason"] = "GitHub API 返回的发布信息结构无效"
            return result

        _remaining(deadline, timeout)
        result.update(ok=True, release=release)
        return result

    except urllib.error.HTTPError as exc:
        try:
            result["status"] = exc.code
            result["etag"] = _safe_etag(exc.headers.get("ETag"))
            if exc.code == 304:
                result.update(
                    ok=True,
                    not_modified=True,
                    etag=result["etag"] or _safe_etag(etag),
                )
            else:
                result.update(_http_failure(exc.code, exc.headers))
            return result
        finally:
            exc.close()
    except (
        urllib.error.URLError, OSError, ValueError, TypeError,
        OverflowError, HTTPException,
    ) as exc:
        result["reason"] = _network_reason(exc, "检查更新")
        _log(result["reason"])
        return result


def _notify_progress(callback, downloaded, total):
    if callback is None:
        return
    try:
        callback(downloaded, total)
    except Exception:
        # 界面回调异常不能使一个正确下载的文件变成损坏文件。
        _log("进度回调执行失败，下载继续")


def download_asset(
    url,
    dest_path,
    expected_sha256,
    timeout=15,
    budget=600,
    on_progress=None,
) -> dict:
    """
    下载附件并校验后提交。

    expected_sha256 为 None 或空串时，仅允许下载 SHA256 清单。
    返回 path、sha256、downloaded、total 及网络错误分类字段。
    """
    result = {
        "ok": False,
        "status": 0,
        "path": "",
        "sha256": "",
        "downloaded": 0,
        "total": None,
        "reason": "",
        "retry_after": None,
        "rate_limited": False,
    }
    part_path = None
    owns_part = False

    try:
        timeout = _positive_seconds(timeout)
        budget = _positive_seconds(budget)
        deadline = time.monotonic() + budget
        parsed = _check_url(url, download=True, initial=True)
        filename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
        is_checksum_list = filename == CHECKSUM_ASSET_NAME

        if expected_sha256 is None or expected_sha256 == "":
            if not is_checksum_list:
                raise ValueError("更新包必须提供预期 SHA256")
            expected = ""
        elif (
            not isinstance(expected_sha256, str)
            or _SHA256_RE.fullmatch(expected_sha256) is None
        ):
            raise ValueError("预期 SHA256 必须是 64 位十六进制字符串")
        else:
            expected = expected_sha256.lower()

        if not is_checksum_list and not (
            filename.startswith(ASSET_ZIP_PREFIX)
            and filename.endswith(".zip")
        ):
            raise ValueError("附件名称不是更新包或校验清单")

        limit = _CHECKSUM_LIMIT if is_checksum_list else _DOWNLOAD_LIMIT
        destination = os.path.abspath(os.fspath(dest_path))
        if not isinstance(destination, str):
            raise ValueError("下载目标必须是文本路径")
        if os.path.isdir(destination):
            raise ValueError("下载目标不能是目录")
        paths.ensure_writable(os.path.dirname(destination))
        part_path = destination + ".part"
        _remaining(deadline, timeout)

        # 下载接口没有当前版本参数，明确使用 unknown，避免猜测程序版本。
        headers = _headers("unknown")
        headers["Accept"] = "application/octet-stream"
        request = urllib.request.Request(url, headers=headers)
        opener = _opener(True, deadline, timeout)

        with opener.open(
            request, timeout=_remaining(deadline, timeout)
        ) as response:
            result["status"] = response.getcode()
            if result["status"] != 200:
                result.update(
                    _http_failure(
                        result["status"], response.headers, download=True
                    )
                )
                return result

            total = _content_length(response.headers)
            result["total"] = total
            if total is not None and total > limit:
                raise ValueError("发布附件超过下载大小上限")

            content_encoding = str(
                response.headers.get("Content-Encoding", "identity")
            ).lower().strip()
            if content_encoding not in {"", "identity"}:
                raise ValueError("发布附件使用了不支持的传输压缩")

            hasher = hashlib.sha256()
            # 独占创建；已有 .part 可能属于另一个下载任务，不能覆盖或删除。
            with io.open(
                part_path, "x", encoding="utf-8", newline="\n"
            ) as stream:
                owns_part = True
                _notify_progress(on_progress, 0, total)
                while True:
                    chunk = _read_chunk(
                        response,
                        min(_CHUNK_SIZE, limit - result["downloaded"] + 1),
                        deadline,
                        timeout,
                    )
                    if not chunk:
                        break
                    if result["downloaded"] + len(chunk) > limit:
                        raise ValueError("发布附件超过下载大小上限")
                    stream.buffer.write(chunk)
                    hasher.update(chunk)
                    result["downloaded"] += len(chunk)
                    _notify_progress(
                        on_progress, result["downloaded"], total
                    )

                if (
                    total is not None
                    and result["downloaded"] != total
                ):
                    raise ValueError("发布附件未完整接收")
                if result["downloaded"] == 0:
                    raise ValueError("发布附件为空")

                result["sha256"] = hasher.hexdigest()
                if expected and not hmac.compare_digest(
                    result["sha256"], expected
                ):
                    raise ValueError("发布附件 SHA256 校验失败")

                _remaining(deadline, timeout)
                stream.flush()
                os.fsync(stream.fileno())

        _remaining(deadline, timeout)
        os.replace(part_path, destination)
        owns_part = False
        result.update(ok=True, path=destination)
        return result

    except urllib.error.HTTPError as exc:
        try:
            result.update(
                _http_failure(exc.code, exc.headers, download=True)
            )
            return result
        finally:
            exc.close()
    except (
        urllib.error.URLError, OSError, ValueError, TypeError,
        RuntimeError, OverflowError, HTTPException,
    ) as exc:
        if isinstance(exc, FileExistsError):
            result["reason"] = "下载临时文件已存在，请先处理已有下载任务"
        elif isinstance(exc, RuntimeError):
            result["reason"] = str(exc)
        else:
            result["reason"] = _network_reason(exc, "下载附件")
        _log(result["reason"])
        return result
    finally:
        if owns_part and part_path is not None:
            try:
                os.unlink(part_path)
            except FileNotFoundError:
                pass
            except OSError:
                _log("无法删除本次下载的临时文件")
