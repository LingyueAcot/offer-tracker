"""
更新规则的纯函数层。

放在 core/，是为了让版本比较、附件选择、校验清单和压缩包路径检查
能够离线测试；这里不读取文件、网络、环境变量或时钟。

关键取舍：
- 使用严格的三段式语义版本，支持预发布标记和构建信息；非法版本不可比。
- 自动更新只选择正式发布，ZIP 名称必须与发布版本完全对应。
- 校验清单采用严格的 GNU SHA256SUMS 格式，任一行异常即整份拒绝。
- ZIP 路径按 Windows 的大小写和名称限制检查，拒绝所有符号链接，
  比只拒绝指向目录外的链接更保守。
- 限流时间采用调用方提供的时间戳，不在这一层决定检查周期。

需要避开的坑：
- 构建信息不影响版本先后，预发布版本小于同版本的正式版。
- Windows 的反斜杠、盘符、保留名称、尾随空格和尾随点都可能绕过简单检查。
- 仅传入文件名字符串无法判断 ZIP 符号链接；需要传入 ZipInfo、
  含 external_attr/mode 的字典，或 (文件名, Unix 模式) 二元组。
"""

import math
import re
import stat
import unicodedata


ASSET_ZIP_PREFIX = "offer-tracker-portable-"
CHECKSUM_ASSET_NAME = "release-SHA256SUMS.txt"
# ⚠️ 校验清单的上限**不能按"只列几个文件"来设**（2026-10-19 实测踩到）：
#    我们的清单是拿**整个包**（约 1940 个文件）算的，实际 **224.8 KB**；
#    原来上限写 32 KiB → 程序把自己的清单判成"大小无效"→ **检查更新永远认不出资产**。
#    现在按 1 MiB 设（留足余量），同时限制到 1 MiB 也够挡住恶意大文件。
CHECKSUM_MAX_BYTES = 1024 * 1024

_VERSION_RE = re.compile(
    r"^(?:v|V)?"
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CHECKSUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})[ \t]+(\*?)(.+)$")
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
    "com¹", "com²", "com³", "lpt¹", "lpt²", "lpt³",
}
_INVALID_WINDOWS_CHARS = frozenset('<>:"|?*')


def _version_parts(text):
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > 128:
        return None

    match = _VERSION_RE.fullmatch(text)
    if match is None:
        return None

    major, minor, patch, prerelease, build = match.groups()
    identifiers = prerelease.split(".") if prerelease else []
    if any(
        item.isascii()
        and item.isdigit()
        and len(item) > 1
        and item.startswith("0")
        for item in identifiers
    ):
        return None

    normalized = f"{major}.{minor}.{patch}"
    if prerelease:
        normalized += f"-{prerelease}"
    if build:
        normalized += f"+{build}"
    return normalized, (int(major), int(minor), int(patch)), identifiers


def normalize_version(text) -> str:
    """返回规范版本；支持 v 前缀、预发布和构建信息，非法输入返回空串。"""
    parsed = _version_parts(text)
    return parsed[0] if parsed else ""


def compare_versions(a, b) -> int:
    """返回 -1、0 或 1；任何一方非法时返回 0，表示不可比。"""
    left = _version_parts(a)
    right = _version_parts(b)
    if left is None or right is None:
        return 0

    if left[1] != right[1]:
        return 1 if left[1] > right[1] else -1

    left_pre, right_pre = left[2], right[2]
    if not left_pre and not right_pre:
        return 0
    if not left_pre:
        return 1
    if not right_pre:
        return -1

    for left_item, right_item in zip(left_pre, right_pre):
        if left_item == right_item:
            continue
        left_numeric = left_item.isascii() and left_item.isdigit()
        right_numeric = right_item.isascii() and right_item.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_item) > int(right_item) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_item > right_item else -1

    if len(left_pre) == len(right_pre):
        return 0
    return 1 if len(left_pre) > len(right_pre) else -1


def is_newer(candidate, current) -> bool:
    """只有两个版本都合法且候选版本更高时才返回 True。"""
    return bool(
        normalize_version(candidate)
        and normalize_version(current)
        and compare_versions(candidate, current) > 0
    )


def _valid_asset(asset, name, size_limit):
    if not isinstance(asset, dict) or asset.get("name") != name:
        return False

    size = asset.get("size")
    if isinstance(size, bool) or not isinstance(size, int):
        return False
    if not 0 < size <= size_limit:
        return False
    if asset.get("state", "uploaded") != "uploaded":
        return False

    url = asset.get("browser_download_url")
    if not isinstance(url, str):
        return False

    # 附件来源限于本项目的 HTTPS 发布下载地址。
    pattern = (
        r"https://github\.com/LingyueAcot/offer-tracker/"
        r"releases/download/[^/?#]+/" + re.escape(name)
    )
    return re.fullmatch(pattern, url, flags=re.IGNORECASE) is not None


def select_assets(
    release: dict, max_size=100 * 1024 * 1024
) -> tuple[dict | None, dict | None, str]:
    """选择与正式发布版本对应的 ZIP 和校验清单；失败时两者均为 None。"""
    if not isinstance(release, dict):
        return None, None, "发布信息格式错误"
    if (
        isinstance(max_size, bool)
        or not isinstance(max_size, int)
        or max_size <= 0
    ):
        return None, None, "下载大小上限无效"
    if release.get("draft") or release.get("prerelease"):
        return None, None, "自动更新不使用草稿或预发布版本"

    version = normalize_version(release.get("tag_name"))
    if not version or _version_parts(version)[2]:
        return None, None, "发布版本无效或属于预发布版本"

    assets = release.get("assets")
    if not isinstance(assets, list):
        return None, None, "发布附件列表格式错误"

    zip_name = f"{ASSET_ZIP_PREFIX}{version}.zip"
    zip_matches = [
        asset for asset in assets
        if isinstance(asset, dict) and asset.get("name") == zip_name
    ]
    sums_matches = [
        asset for asset in assets
        if isinstance(asset, dict)
        and asset.get("name") == CHECKSUM_ASSET_NAME
    ]

    if len(zip_matches) != 1:
        return None, None, "对应版本的更新包缺失或重名"
    if len(sums_matches) != 1:
        return None, None, "SHA256 校验清单缺失或重名"
    if not _valid_asset(zip_matches[0], zip_name, max_size):
        return None, None, "更新包大小、状态或下载地址无效"
    if not _valid_asset(sums_matches[0], CHECKSUM_ASSET_NAME, CHECKSUM_MAX_BYTES):
        return None, None, "校验清单大小、状态或下载地址无效"

    return zip_matches[0], sums_matches[0], ""


def _component_reason(component):
    if not component or component in {".", ".."}:
        return "路径包含空名称、当前目录或上级目录"
    if component.endswith((" ", ".")):
        return "Windows 名称不能以空格或点结尾"
    if any(
        ord(char) < 32 or ord(char) == 127 or char in _INVALID_WINDOWS_CHARS
        for char in component
    ):
        return "路径包含 Windows 禁止的字符"
    if component.split(".", 1)[0].casefold() in _RESERVED_NAMES:
        return "路径使用了 Windows 保留名称"
    return ""


def _filename_valid(filename):
    return (
        isinstance(filename, str)
        and bool(filename)
        and filename == filename.strip()
        and "/" not in filename
        and "\\" not in filename
        and not _component_reason(filename)
    )


def parse_checksum_list(text, limit=CHECKSUM_MAX_BYTES) -> dict:
    """
    解析 UTF-8 文本的 GNU 校验清单。

    接受「哈希  文件名」和「哈希 *文件名」、CRLF 和首行 BOM。
    大小上限按 UTF-8 字节计算；仅接受不带路径的文件名。
    """
    if (
        not isinstance(text, str)
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit <= 0
        or len(text) > limit
    ):
        return {}
    try:
        if len(text.encode("utf-8")) > limit:
            return {}
    except UnicodeError:
        return {}

    text = text.removeprefix("\ufeff")
    result = {}
    seen = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _CHECKSUM_LINE_RE.fullmatch(line)
        if match is None:
            return {}
        digest, _, filename = match.groups()
        if not _filename_valid(filename):
            return {}
        key = unicodedata.normalize("NFC", filename).casefold()
        if key in seen:
            return {}
        seen.add(key)
        result[filename] = digest.lower()
    return result


def checksum_for(checksums: dict, filename: str) -> str:
    """按精确文件名取合法 SHA256，不进行模糊匹配或大小写补偿。"""
    if not isinstance(checksums, dict) or not _filename_valid(filename):
        return ""
    value = checksums.get(filename)
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        return ""
    return value.lower()


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) else None


def decide_check(
    allowed_at: float, now: float, last_check=None
) -> tuple[bool, str]:
    """
    判断是否已经到达允许检查的时间。

    三个时间均采用同一时间基准的秒数；last_check 可为空。
    state.json 中的日期字符串应由调用方转换后传入。
    """
    allowed = _finite_number(allowed_at)
    current = _finite_number(now)
    previous = None if last_check is None else _finite_number(last_check)

    if allowed is None or current is None:
        return False, "检查时间参数无效"
    if last_check is not None and previous is None:
        return False, "上次检查时间无效"
    if previous is not None and current < previous:
        return False, "系统时间早于上次检查，请稍后重试"
    if current < allowed:
        seconds = math.ceil(allowed - current)
        return False, f"尚未到允许检查的时间，请在 {seconds} 秒后重试"
    return True, ""


def backoff_seconds(fail_count) -> int:
    """第 1 次失败等待 60 秒，此后翻倍，最多 3600 秒。"""
    if isinstance(fail_count, bool) or not isinstance(fail_count, int):
        return 60
    if fail_count <= 1:
        return 60
    if fail_count >= 7:
        return 3600
    return 60 * (2 ** (fail_count - 1))


def _entry_metadata(entry):
    if isinstance(entry, str):
        return entry, 0, False
    if isinstance(entry, tuple) and len(entry) == 2:
        name, unix_mode = entry
        if isinstance(unix_mode, bool) or not isinstance(unix_mode, int):
            raise ValueError("ZIP 模式字段无效")
        return name, unix_mode, False
    if isinstance(entry, dict):
        name = entry.get("filename", entry.get("name"))
        explicit_mode = entry.get("mode")
        external_attr = entry.get("external_attr", 0)
        marked_link = (
            bool(entry.get("is_symlink"))
            or entry.get("link_target") is not None
        )
    else:
        name = getattr(entry, "filename", None)
        explicit_mode = None
        external_attr = getattr(entry, "external_attr", 0)
        marked_link = False

    if explicit_mode is not None:
        if isinstance(explicit_mode, bool) or not isinstance(explicit_mode, int):
            raise ValueError("ZIP 模式字段无效")
        unix_mode = explicit_mode
    else:
        if isinstance(external_attr, bool) or not isinstance(external_attr, int):
            raise ValueError("ZIP 属性字段无效")
        unix_mode = (external_attr >> 16) & 0xFFFF
    return name, unix_mode, marked_link


def validate_zip_entries(names) -> tuple[bool, str]:
    """
    校验 ZIP 条目名称及可用的 Unix 属性。

    不读取链接内容；带符号链接标记的条目一律拒绝。
    另外拒绝特殊文件、文件与目录冲突及超过 20000 个条目的压缩包。
    """
    if isinstance(names, (str, bytes)) or names is None:
        return False, "ZIP 条目列表格式错误"

    entries = {}
    try:
        for index, entry in enumerate(names):
            if index >= 20000:
                return False, "ZIP 条目数量超过安全上限"

            name, unix_mode, marked_link = _entry_metadata(entry)
            if not isinstance(name, str) or not name or len(name) > 1024:
                return False, "ZIP 条目名称无效或过长"

            file_type = stat.S_IFMT(unix_mode)
            if marked_link or file_type == stat.S_IFLNK:
                return False, "更新包不能包含符号链接"
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                return False, "更新包不能包含特殊文件"

            normalized = name.replace("\\", "/")
            if normalized.startswith("/") or ":" in normalized:
                return False, "ZIP 条目包含绝对路径、盘符或数据流名称"

            is_directory = normalized.endswith("/")
            normalized = normalized[:-1] if is_directory else normalized
            components = normalized.split("/")
            for component in components:
                reason = _component_reason(component)
                if reason:
                    return False, reason

            if file_type == stat.S_IFDIR and not is_directory:
                return False, "ZIP 目录属性与名称不一致"
            if file_type == stat.S_IFREG and is_directory:
                return False, "ZIP 文件属性与名称不一致"

            key = "/".join(
                unicodedata.normalize("NFC", component).casefold()
                for component in components
            )
            if key in entries:
                return False, "ZIP 条目存在大小写重名或文件目录重名"
            entries[key] = is_directory
    except (TypeError, ValueError, AttributeError, OverflowError):
        return False, "ZIP 条目或属性格式错误"

    if not entries:
        return False, "ZIP 条目列表为空"

    for key in entries:
        components = key.split("/")
        for index in range(1, len(components)):
            parent = "/".join(components[:index])
            if parent in entries and not entries[parent]:
                return False, "ZIP 文件被其他条目当作目录使用"
    return True, ""
