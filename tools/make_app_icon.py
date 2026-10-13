# -*- coding: utf-8 -*-
"""
把一张源图转成程序真正用的图标：`app/web/app.ico`（**会把背景抠成透明**）。

用法：

    python tools\\make_app_icon.py <源图路径> [--keep-bg]

【为什么会需要一个脚本，而不是手工"另存为 .ico"】

1. **图标必须在程序内部**：`app\\web\\app.ico`。桌面快捷方式的 IconLocation 和运行时窗口图标
   都按 `BASE_DIR` 拼路径读它 —— 绝不引用源图所在的位置（源图一般躺在下载目录，
   被挪走/清掉就会"图标莫名其妙消失"）。生成完源图删掉都不影响程序。
2. **背景必须抠掉**（2026-10-10 用户报「显示不出图标 / 不是透明背景」之后加的）：
   源图是 **JPEG**（jpg 结构上就没有 alpha 通道），而它边缘是一圈 **灰白棋盘格**
   （202 灰 / 253 白交替，约 40px 一块）——AI 出图把"透明"画成了棋盘格再存成 JPG。
   不抠的话缩到 16×16（任务栏）就是一片浅色噪点，看起来"没有图标"。
   抠法：从**四条边**泛洪，只把「与边界相连、且低饱和 + 亮」的像素变透明 ——
   主体内部同色的白/灰（高光、白字、眼白）不与边界相连，会原样保留。
   源图本来就是透明 PNG 时自动跳过这一步（已经透明就不用抠）；要显式关掉用 `--keep-bg`。
3. **一个 .ico 里装七种尺寸**（16/24/32/48/64/128/256），而且**小尺寸用 BMP 帧、256 用 PNG 帧**：
   这是 Windows 的传统布局（BMP/DIB 帧所有外壳都认；256 用 PNG 才不会让文件爆大）。
   只塞一张大图，任务栏缩到 16px 会糊成一团。
4. **换图有据可依**：第一版图标是 一个一次性脚本 从 SVG 生成的；
   2026-10-10 换成用户给的图（先只转格式，发现有棋盘格背景，再加的抠背景）。

⚠️ 这个脚本**只在开发用的电脑上跑**（需要 Pillow）。程序本身不依赖 Pillow —— 它只读那个 .ico 文件。
"""
import io
import os
import struct
import sys

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageMath
except ImportError:  # pragma: no cover - 只在没装 Pillow 的开发用的电脑上触发
    print("需要 Pillow：pip install pillow（只有开发用的电脑需要，程序本身不依赖它）")
    sys.exit(2)

# 七种尺寸一次装齐；**小尺寸走 BMP 帧、256 走 PNG 帧**（见文件头说明）
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
PNG_FROM = 256          # 只有 256 用 PNG 帧
FILLED = 128            # 泛洪填充时给"背景"打的标记值

# 背景判定：低饱和（彩色差 ≤ 18）且够亮（最暗通道 ≥ 185）—— 灰白棋盘格两条都满足
SAT_MAX = 18
DARK_MIN = 185

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "app", "web", "app.ico")


# --------------------------------------------------------------------------- #
# 抠背景
# --------------------------------------------------------------------------- #
def has_real_alpha(img):
    """源图是不是真有透明（PNG 那种）：有任何一个像素的 alpha < 250 就算。"""
    if img.mode not in ("RGBA", "LA", "PA"):
        return False
    alpha = img.convert("RGBA").getchannel("A")
    return alpha.getextrema()[0] < 250


def background_mask(img):
    """把"低饱和 + 亮"的像素标成 255，其余 0（纯色块判定，不含连通性）。"""
    rgb = img.convert("RGB")
    r, g, b = rgb.split()
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
    mn = ImageChops.darker(ImageChops.darker(r, g), b)
    sat = ImageChops.subtract(mx, mn)
    return ImageChops.darker(sat.point(lambda v: 255 if v <= SAT_MAX else 0),
                             mn.point(lambda v: 255 if v >= DARK_MIN else 0))


def cut_background(img):
    """
    从四条边泛洪，把**与边界相连**的浅色区抠成透明。返回 (新图, 透明像素数, 占比)。

    为什么是"从边界泛洪"而不是"把所有浅色都抠掉"：主体内部的高光、白字、眼白同样是浅色，
    但它们不与画布边界相连 —— 只抠连通到边界的那一片，画面才不会被抠出洞。
    """
    rgba = img.convert("RGBA")
    width, height = rgba.size
    mask = background_mask(rgba)
    seeds = []
    for x in range(width):
        seeds.append((x, 0))
        seeds.append((x, height - 1))
    for y in range(height):
        seeds.append((0, y))
        seeds.append((width - 1, y))
    for point in seeds:
        if mask.getpixel(point) == 255:          # 还没被填过、且是背景色
            ImageDraw.floodfill(mask, point, FILLED, thresh=0)
    alpha = mask.point(lambda v: 0 if v == FILLED else 255)
    # 边缘轻微羽化：泛洪是硬边，缩到 16px 会有锯齿；0.8px 的模糊只影响最外一圈
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.8))
    rgba.putalpha(alpha)
    hist = alpha.histogram()
    transparent = sum(hist[:8])
    return rgba, transparent, transparent / float(width * height)


# --------------------------------------------------------------------------- #
# 写 ICO（自己拼：小尺寸 BMP 帧 + 256 PNG 帧）
# --------------------------------------------------------------------------- #
def unpremultiply(channel, alpha):
    """把预乘过的单通道除回 alpha（ImageMath 逐像素算；convert 会自己夹到 0~255）。

    ⚠️ 名字有历史包袱：老 Pillow 是 `ImageMath.eval`，10.3 起改名 `unsafe_eval`，
    12 里只剩新的名字了 —— 两个都试，免得换个 Pillow 版本就报 AttributeError。
    """
    expression = "convert(a * 255 / b, 'L')"
    for name in ("unsafe_eval", "eval"):
        func = getattr(ImageMath, name, None)
        if func is not None:
            return func(expression, a=channel, b=alpha)
    raise RuntimeError("这个 Pillow 既没有 ImageMath.unsafe_eval 也没有 eval")


def tight_square(img, pad_ratio=0.06):
    """
    把主体裁到最小，再**补成正方形**（多出来的那一边用透明填），返回新图。

    为什么：抠掉背景后主体外面还剩一大圈透明（实测 57.8% 是透明），直接缩到 16×16
    主体只剩中间一小块 —— 图标看着"很小、像没有"。裁紧一点、留 6% 边距，
    再补成正方形（不能直接缩非正方形的图，会被压变形）。
    """
    box = img.getchannel("A").getbbox()
    if not box:
        return img
    left, top, right, bottom = box
    span = max(right - left, bottom - top)
    pad = int(span * pad_ratio)
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(img.size[0], right + pad)
    bottom = min(img.size[1], bottom + pad)
    side = max(right - left, bottom - top)
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    piece = img.crop((left, top, right, bottom))
    out.paste(piece, ((side - piece.size[0]) // 2, (side - piece.size[1]) // 2))
    return out


def bmp_frame(img):
    """一张 32 位 BGRA 的 DIB 帧（BITMAPINFOHEADER + 自下而上的像素 + 全零 AND 掩码）。"""
    width, height = img.size
    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0,
                         width * height * 4, 0, 0, 0, 0)
    pixels = img.convert("RGBA").transpose(Image.FLIP_TOP_BOTTOM).tobytes("raw", "BGRA")
    stride = ((width + 31) // 32) * 4
    and_mask = b"\x00" * (stride * height)      # 32 位帧靠 alpha 通道，掩码全零
    return header + pixels + and_mask


def write_ico(frames, path):
    """frames: [(size, PIL 图)] → 按 Windows 传统布局写一个 .ico。"""
    entries, blobs = [], []
    offset = 6 + 16 * len(frames)
    for size, img in frames:
        if size[0] >= PNG_FROM:
            import io as _io
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
            blob = buf.getvalue()
        else:
            blob = bmp_frame(img)
        entries.append(struct.pack("<BBBBHHII", size[0] % 256, size[1] % 256,
                                   0, 0, 1, 32, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    with open(path, "wb") as handle:
        handle.write(struct.pack("<HHH", 0, 1, len(frames)))
        for entry in entries:
            handle.write(entry)
        for blob in blobs:
            handle.write(blob)


def resize_premultiplied(img, size):
    """
    带 alpha 的缩图要用**预乘**再还原，否则半透明边缘会和黑/白背景混出脏边。

    （LANCZOS 直接缩 RGBA 时，透明像素的 RGB 是黑的，会往边缘渗色；先乘 alpha、
      缩完再除回去，颜色就只来自真正不透明的那部分。）
    """
    r, g, b, a = img.split()
    pr = ImageChops.multiply(r, a)
    pg = ImageChops.multiply(g, a)
    pb = ImageChops.multiply(b, a)
    pre = Image.merge("RGBA", (pr, pg, pb, a)).resize(size, Image.LANCZOS)
    pr, pg, pb, a = pre.split()
    a2 = a.point(lambda v: 255 if v == 0 else v)     # 除零保护（全透明像素的 RGB 无所谓）
    out = Image.merge("RGBA", (unpremultiply(pr, a2), unpremultiply(pg, a2),
                               unpremultiply(pb, a2), a))
    return out


def ico_report(path):
    """把写出来的 .ico 读回来验一遍：帧数、尺寸、每帧是 BMP 还是 PNG。"""
    blob = open(path, "rb").read()
    count = struct.unpack_from("<H", blob, 4)[0]
    rows = []
    for i in range(count):
        off = 6 + i * 16
        width = blob[off] or 256
        height = blob[off + 1] or 256
        size, data_off = struct.unpack_from("<II", blob, off + 8)
        head = blob[data_off:data_off + 8]
        kind = "PNG" if head[:4] == b"\x89PNG" else ("BMP" if struct.unpack_from("<I", head, 0)[0] == 40 else "?")
        rows.append((width, height, kind, size))
    return rows


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    keep_bg = "--keep-bg" in argv[1:]
    if not args:
        print(__doc__)
        return 1
    src = args[0]
    if not os.path.isfile(src):
        print("找不到源图：%s" % src)
        return 1

    img = Image.open(src).convert("RGBA")
    width, height = img.size
    print("源图：%s（%dx%d，%s）" % (src, width, height, Image.open(src).format))
    if width != height:
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        img = img.crop((left, top, left + side, top + side))
        print("  不是方图，已居中裁成 %dx%d" % (side, side))

    if has_real_alpha(img):
        print("  源图本来就有透明通道 —— 跳过抠背景")
    elif keep_bg:
        print("  --keep-bg：按要求保留背景（不抠）")
    else:
        img, transparent, ratio = cut_background(img)
        print("  抠背景：从四边泛洪，透明像素 %.1f%%（只抠了与边界相连的浅色区，"
              "主体内部的浅色细节保留）" % (ratio * 100))

    if not keep_bg:
        before = img.size
        img = tight_square(img)
        print("  裁到主体并补成正方形：%dx%d → %dx%d" % (before[0], before[1], img.size[0], img.size[1]))

    frames = [(size, resize_premultiplied(img, size)) for size in SIZES]
    out_dir = os.path.dirname(OUT)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    write_ico(frames, OUT)
    print("已写出：%s（%.1f KB）" % (OUT, os.path.getsize(OUT) / 1024.0))

    rows = ico_report(OUT)
    print("实际帧：%s" % "、".join("%dx%d(%s)" % (w, h, k) for w, h, k, _s in rows))
    missing = [s for s in SIZES if (s[0], s[1]) not in [(w, h) for w, h, _k, _s in rows]]
    if missing:
        print("⚠️ 少了：%s" % missing)
        return 1
    # 透明度自检：256 帧的四个角必须是透明的（否则说明背景没抠掉）
    big = frames[-1][1]
    corners = [big.getpixel((2, 2))[3], big.getpixel((253, 2))[3],
               big.getpixel((2, 253))[3], big.getpixel((253, 253))[3]]
    print("256 帧四角的 alpha：%s %s" % (corners, "（透明 ✓）" if max(corners) < 40 else "（★不透明，背景没抠掉）"))
    if max(corners) >= 40 and not (has_real_alpha(Image.open(src)) or keep_bg):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
