# -*- coding: utf-8 -*-
"""
core/resume_photo.py —— 证件照的纯校验（不依赖 Pillow，自己走一遍 JPEG 结构）

【为什么自己解析】
程序是**零依赖**的（后端纯标准库），不能装 Pillow。JPEG 的宽高就在 SOF 段里，
按 marker 走一遍就能读到；顺带把结构完整性也验了（SOI → 各段 → SOS → EOI），
坏文件挡在入库之前。

【边界（写在明处）】这里只做**结构与尺寸校验**，不解码像素 ——
所以"结构合法但像素损坏"的图仍可能通过；不把它说成"完整解码校验"。

【规矩】core/ 纯函数：只许标准库里的纯计算模块（base64 / binascii / re），
不许 os/io/json/pathlib/sqlite3/urllib，也不许 open() 文件。
"""
import base64
import binascii
import re

MIME = "image/jpeg"
MAX_BYTES = 262144              # 解码后 ≤256 KiB（外部评审 定的契约）
MAX_SIDE = 600                  # 宽、高各 ≤600
MAX_BASE64_CHARS = 349528       # 262144 字节对应的 base64 字符上限（含填充）

DATA_URL_RE = re.compile(r"^data:image/jpe?g;base64,", re.I)
B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

# SOF 段（含宽高）与只支持的几种：C0 基线 / C1 扩展顺序 / C2 渐进
SOF_MARKERS = (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
SUPPORTED_SOF = (0xC0, 0xC1, 0xC2)


def decode_base64(text):
    """严格解码 base64。返回 (bytes, 错误)：拒绝非 ASCII / 非法字符 / 错误填充 / data-URL 前缀。"""
    if not isinstance(text, str):
        return None, "照片数据必须是字符串"
    value = text.strip()
    if not value:
        return None, "照片数据是空的"
    if DATA_URL_RE.match(value):
        return None, "照片不要带 data: 前缀（只存 base64 本体）"
    if len(value) > MAX_BASE64_CHARS:
        return None, "照片太大（编码后 %d 字符，上限 %d）" % (len(value), MAX_BASE64_CHARS)
    if not B64_RE.match(value):
        return None, "照片数据不是合法的 base64"
    if len(value) % 4:
        return None, "照片 base64 长度不是 4 的倍数（填充不对）"
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None, "照片 base64 解码失败"
    if not raw:
        return None, "照片解码后是空的"
    if len(raw) > MAX_BYTES:
        return None, "照片超过 256KB（解码后 %d 字节）" % len(raw)
    return raw, None


def jpeg_size(data):
    """解析 JPEG 结构并读宽高。返回 ((宽, 高), 错误)。"""
    if not data or data[0:2] != b"\xff\xd8":
        return None, "不是 JPEG（缺少 SOI 头）"
    index = 2
    total = len(data)
    while index + 4 <= total:
        if data[index] != 0xFF:
            return None, "JPEG 结构异常（段标记丢失）"
        marker = data[index + 1]
        index += 2
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9:
            break
        if index + 2 > total:
            return None, "JPEG 结构异常（段长度缺失）"
        length = (data[index] << 8) | data[index + 1]
        if length < 2 or index + length > total:
            return None, "JPEG 结构异常（段长度越界）"
        if marker in SOF_MARKERS:
            if length < 7:
                return None, "JPEG 的 SOF 段太短"
            height = (data[index + 3] << 8) | data[index + 4]
            width = (data[index + 5] << 8) | data[index + 6]
            if width < 1 or height < 1:
                return None, "JPEG 宽高不合法"
            if width > MAX_SIDE or height > MAX_SIDE:
                return None, "照片尺寸超过 %d×%d（实际 %d×%d）" % (MAX_SIDE, MAX_SIDE, width, height)
            if marker not in SUPPORTED_SOF:
                return None, "只支持常见的 8 位 JPEG（基线 / 渐进）"
            return (width, height), None
        if marker == 0xDA:
            return None, "JPEG 里找不到尺寸信息（SOF 在扫描数据之后）"
        index += length
    return None, "JPEG 不完整（缺少尺寸段或结束标记）"


def validate(photo):
    """
    校验并归一化一张证件照。返回 (photo, 错误)：
        {"mime": "image/jpeg", "data_base64": "…", "width": w, "height": h}

    `None` / 没有 data_base64 → (None, None)：**没有照片是合法状态**。
    """
    if photo is None:
        return None, None
    if not isinstance(photo, dict):
        return None, "photo 必须是对象"
    if not photo.get("data_base64"):
        return None, None
    mime = str(photo.get("mime") or MIME).lower()
    if mime not in ("image/jpeg", "image/jpg"):
        return None, "只支持 JPEG 照片"
    raw, error = decode_base64(photo.get("data_base64"))
    if error:
        return None, error
    size, error = jpeg_size(raw)
    if error:
        return None, error
    width, height = size
    declared_width, declared_height = photo.get("width"), photo.get("height")
    if declared_width not in (None, "", 0):
        try:
            same = int(declared_width) == width and int(declared_height) == height
        except (TypeError, ValueError):
            same = False
        if not same:
            return None, "照片声明的尺寸与实际不符"
    return {"mime": MIME, "data_base64": base64.b64encode(raw).decode("ascii"),
            "width": width, "height": height}, None


def summary(photo):
    """给审计日志用的摘要（**绝不包含 base64 本体**）。"""
    if not photo:
        return None
    return {"mime": photo.get("mime"), "width": photo.get("width"), "height": photo.get("height")}
