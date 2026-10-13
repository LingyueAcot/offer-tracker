# -*- coding: utf-8 -*-
"""
tools/test_resume_photo.py —— 证件照校验的自检（纯逻辑，不用开程序、不碰真库）

查 `core/resume_photo.py`（base64 严格解码 + JPEG 结构/尺寸）与它在
`core/resume.py` 归一化里的接入：坏照片必须在**入库之前**被挡下，且报错要说清原因。

夹具（tools/fixtures/，用开发用的电脑的 Pillow 造的一次性文件；**程序本身不依赖 Pillow**）：
    photo-ok.jpg            458×600（证件照比例 30.7:40.2）
    photo-big-side.jpg      700×900（超过 600 的尺寸上限）
    photo-wrong-format.png  用来验"只收 JPEG"
"""
import base64
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")
if not os.path.isfile(os.path.join(APP, "server.py")):
    print("[FAIL] 找不到 app/server.py —— 脚本位置推算不对，拒绝继续")
    sys.exit(1)
sys.path.insert(0, APP)

from core import resume as resume_core          # noqa: E402
from core import resume_photo as photo          # noqa: E402

passed, failed = [], []


def check(label, cond, extra=""):
    print(("[PASS] " if cond else "[FAIL] ") + label + (("  " + str(extra)) if extra else ""))
    (passed if cond else failed).append(label)


FIXTURES = os.path.join(HERE, "fixtures")
OK_PATH = os.path.join(FIXTURES, "photo-ok.jpg")
BIG_PATH = os.path.join(FIXTURES, "photo-big-side.jpg")
PNG_PATH = os.path.join(FIXTURES, "photo-wrong-format.png")
ok_bytes = open(OK_PATH, "rb").read() if os.path.isfile(OK_PATH) else b""
ok_b64 = base64.b64encode(ok_bytes).decode("ascii") if ok_bytes else ""

# ---------------------------------------------------------------- 夹具与结构解析
check("夹具在（tools/fixtures/photo-ok.jpg）", bool(ok_bytes), "%d 字节" % len(ok_bytes))
size, error = photo.jpeg_size(ok_bytes)
check("JPEG 结构解析：读出宽高 458×600", error is None and size == (458, 600), "%s %s" % (size, error))
check("尺寸上限 600：超尺寸图被拒绝", photo.jpeg_size(open(BIG_PATH, "rb").read())[1] is not None,
      photo.jpeg_size(open(BIG_PATH, "rb").read())[1])
check("非 JPEG（PNG）被拒绝", photo.jpeg_size(open(PNG_PATH, "rb").read())[1] is not None)
check("空数据被拒绝", photo.jpeg_size(b"")[1] is not None)
check("截断的 JPEG 被拒绝（只留前 120 字节）",
      photo.jpeg_size(ok_bytes[:120])[1] is not None, photo.jpeg_size(ok_bytes[:120])[1])
check("不是 SOI 开头就被拒", photo.jpeg_size(b"\x00\x01\x02\x03")[1] is not None)
check("宽高上限常量 = 600、体积上限 = 256KiB",
      photo.MAX_SIDE == 600 and photo.MAX_BYTES == 262144)

# ---------------------------------------------------------------- base64 严格解码
raw, error = photo.decode_base64(ok_b64)
check("合法 base64 能解码且字节数一致", error is None and raw == ok_bytes, "%s %d" % (error, len(raw or b"")))
check("非字符串被拒绝", photo.decode_base64(12345)[1] is not None)
check("空串被拒绝", photo.decode_base64("   ")[1] is not None)
check("带 data: 前缀被拒绝（只存 base64 本体）",
      "前缀" in (photo.decode_base64("data:image/jpeg;base64,AAAA")[1] or ""))
check("非法字符被拒绝", photo.decode_base64("!!!!")[1] is not None)
check("长度不是 4 的倍数被拒绝", photo.decode_base64(ok_b64 + "A")[1] is not None)
check("超过字符上限被拒绝（先按长度挡下，不解码）",
      photo.decode_base64("A" * (photo.MAX_BASE64_CHARS + 4))[1] is not None)

# ---------------------------------------------------------------- validate（契约）
none_photo, error = photo.validate(None)
check("没有照片是合法状态（None → None, None）", none_photo is None and error is None)
empty_photo, error = photo.validate({})
check("photo 是空对象也算没照片", empty_photo is None and error is None)
check("photo 不是对象被拒绝", photo.validate("x")[1] is not None)
check("非 JPEG 的 mime 被拒绝", photo.validate({"mime": "image/png", "data_base64": ok_b64})[1] is not None)

kept, error = photo.validate({"mime": "image/jpeg", "data_base64": ok_b64,
                              "width": 458, "height": 600})
check("合法照片通过校验并归一化", error is None and kept["width"] == 458 and kept["height"] == 600,
      error)
check("归一化后存的是**标准 base64**（可再解码回原字节）",
      base64.b64decode(kept["data_base64"], validate=True) == ok_bytes)
check("声明的尺寸与实际不符要报错",
      photo.validate({"data_base64": ok_b64, "width": 300, "height": 400})[1] is not None)
check("审计摘要里**不含** base64 本体",
      "data_base64" not in (photo.summary(kept) or {}), photo.summary(kept))

# ---------------------------------------------------------------- 接入 core/resume.py
defaults = resume_core.default_content()
check("默认正文里 photo = None（新简历没有照片）", defaults.get("photo", "缺键") is None)
old, error = resume_core.normalize_content({"basics": {"name": "老王"}})
check("老数据没有 photo 键也能归一化（按没照片处理）", error is None and old["photo"] is None, error)

with_photo, error = resume_core.normalize_content({"photo": {"data_base64": ok_b64}})
check("归一化保留证件照", error is None and with_photo["photo"]["width"] == 458, error)
bad, error = resume_core.normalize_content({"photo": {"data_base64": "!!!bad!!!"}})
check("坏照片让整次保存**失败并给出原因**（挡住入库）",
      bad is None and error and error.startswith("photo："), error)
bad2, error2 = resume_core.normalize_content({"photo": {"mime": "image/png", "data_base64": ok_b64}})
check("非 JPEG 同样被挡在入库之前", bad2 is None and error2 is not None, error2)

print("")
print("共 %d 项：通过 %d · 失败 %d" % (len(passed) + len(failed), len(passed), len(failed)))
print("结果：全部通过" if not failed else "结果：存在失败项")
sys.exit(0 if not failed else 1)
