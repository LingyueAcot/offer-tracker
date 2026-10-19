# -*- coding: utf-8 -*-
"""
services/resume_pptx.py —— 拿内置空白模板当脚手架，生成可编辑的 PPTX 字节

【为什么这么做（而不是从零写全套 OOXML）】
`ppt/theme1.xml`、母版、版式、tableStyles 这些"格式定义"又长又难写对，
而 **已脱敏的空白模板里就有现成的、WPS/PowerPoint 都认的那一套**。
所以：读模板 → 换掉每一页的 `slideN.xml` → 重建 `presentation.xml` /
它的 rels / `[Content_Types].xml` 里跟幻灯片有关的登记 → 重新打包。

⚠️ **多页不能只往 zip 里加文件**：`[Content_Types].xml`、`presentation.xml` 的
`sldIdLst`、`presentation.xml.rels` 必须同步，否则 PowerPoint 打开会报"需要修复"。

【规矩】本文件属于 services/：可以用 os / io / zipfile / re；**不许 import server**。
布局算术全在 `core/resume_pptx.py`（纯函数）里，这里只负责翻译成 XML。
"""
import base64
import binascii
import io
import os
import re
import zipfile
from xml.sax.saxutils import escape

from core import resume_pptx as planner

TEMPLATE_NAME = "resume-template.pptx"
EMU_PER_MM = 36000
PPTX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".presentationml.presentation")

# 文件里要出现的中文字体（和数据里的值无关，是模板本身用的字体）
FONT = "微软雅黑"

DROPPED_PARTS = (
    "ppt/notesSlides/notesSlide1.xml",
    "ppt/notesSlides/_rels/notesSlide1.xml.rels",
    "ppt/notesMasters/notesMaster1.xml",
    "ppt/notesMasters/_rels/notesMaster1.xml.rels",
)


def template_path():
    """内置空白模板的位置：app/web/resume-template.pptx。"""
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(app_dir, "web", TEMPLATE_NAME)


def build(content, layout):
    """把一份简历（正文 + 排版）渲染成 PPTX 字节。"""
    plan = planner.fit_one_page(content, layout)
    theme = plan["theme"]
    pages = plan["pages"]

    # 证件照：只有"要显示照片"时才写 media 部件与图片关系 ——
    # 隐藏/没有照片时**一个字节都不进包**，免得"看不见却藏在文件里"。
    photo_bytes = None
    photo = plan.get("photo")
    if photo and photo.get("data_base64"):
        try:
            photo_bytes = base64.b64decode(photo["data_base64"], validate=True)
        except (binascii.Error, ValueError):
            photo_bytes = None

    with zipfile.ZipFile(template_path()) as source:
        parts = {name: source.read(name) for name in source.namelist() if not name.endswith("/")}

    for name in DROPPED_PARTS:
        parts.pop(name, None)

    for index, page in enumerate(pages, start=1):
        with_photo = bool(photo_bytes) and index == 1
        parts["ppt/slides/slide%d.xml" % index] = _slide_xml(page, theme, plan, with_photo).encode("utf-8")
        parts["ppt/slides/_rels/slide%d.xml.rels" % index] = _slide_rels(with_photo).encode("utf-8")
    if photo_bytes:
        parts["ppt/media/resume-photo.jpg"] = photo_bytes
    # 模板只有 1 张幻灯片；多页时把多余的 slideN 清掉（理论上不会有）
    for name in list(parts):
        match = re.match(r"ppt/slides/slide(\d+)\.xml$", name)
        if match and int(match.group(1)) > len(pages):
            parts.pop(name, None)
            parts.pop("ppt/slides/_rels/slide%s.xml.rels" % match.group(1), None)

    parts["ppt/presentation.xml"] = _presentation_xml(parts["ppt/presentation.xml"], len(pages)).encode("utf-8")
    parts["ppt/_rels/presentation.xml.rels"] = _presentation_rels(len(pages)).encode("utf-8")
    parts["[Content_Types].xml"] = _content_types(parts["[Content_Types].xml"], len(pages),
                                                  bool(photo_bytes)).encode("utf-8")
    parts["docProps/app.xml"] = _app_xml(parts["docProps/app.xml"], len(pages)).encode("utf-8")

    # 兜底：任何 .rels 里指向已删部件的条目一并去掉（否则是"悬空关系"，PowerPoint 会挑刺）
    for name, data in list(parts.items()):
        if not name.endswith(".rels"):
            continue
        text = data.decode("utf-8")
        cleaned = text
        for dropped in DROPPED_PARTS:
            target = dropped.split("/", 1)[1]
            cleaned = re.sub(r'<Relationship [^>]*Target="[^"]*%s"[^>]*/>' % re.escape(target),
                             "", cleaned)
        if cleaned != text:
            parts[name] = cleaned.encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in parts.items():
            target.writestr(name, data)
    return buffer.getvalue()


# ---------------------------------------------------------------- XML 生成

def _emu(mm):
    return int(round(float(mm) * EMU_PER_MM))


def _color(token, theme):
    """把布局里的颜色记号翻成 OOXML 的 srgbClr 值。

    ⚠️ **必须去掉 `#`**：OOXML 的 `<a:srgbClr val="...">` 只认 6 位十六进制，
    带 `#` 是无效值（实测：整块抬头色不生效）。layout.theme 里存的是 `#RRGGBB`（给 CSS 用），
    这里负责转成 `RRGGBB`。
    """
    if token == "accent":
        value = theme["accent"]
    elif token == "header_text":
        value = theme["header_text"]
    elif token == "date":
        value = planner.DATE_COLOR
    elif token == "rule":
        value = planner.RULE_COLOR
    elif token == "divider":
        value = planner.DIVIDER_COLOR
    elif token == "band_rule":
        value = planner.BAND_RULE_COLOR
    else:
        value = token or planner.TEXT_COLOR
    return str(value).lstrip("#").upper()


def _text_box(shape_id, rows, theme, plan):
    """一个文本框装**一整块内容**（板块标题 + 它的各条目），内部用多个段落。

    为什么不再"一行一个文本框"（2026-10-11 改）：那样每行的纵向位置全靠我自己估算"占多高"，
    估多了出现大空隙（用户报「间隔太远」）、估少了压字串行（用户报「错乱了」）。
    现在段内换行与段间距都交给 PowerPoint 按**精确行距/段距**排，不再依赖估算。
    """
    first = rows[0]
    height = sum(row.get("height_mm") or 0 for row in rows) or 4.0
    paragraphs = []
    for row in rows:
        size = row.get("size_pt") or planner.SIZE_BODY
        line_height = row.get("line_height") or plan.get("line_height") or 1.55
        # 抬头那几行是**单行、绝对定位**的，不需要 LINE_MIN 那个"防切字"下限
        floor = 1.0 if row.get("fixed") else planner.LINE_MIN
        line_pt = max(100, int(round(size * max(floor, line_height) * 100)))
        after_pt = max(0, int(round((row.get("space_after_mm") or 0) / planner.MM_PER_PT * 100)))
        paragraphs.append(
            '<a:p><a:pPr algn="l"><a:lnSpc><a:spcPts val="%d"/></a:lnSpc>'
            '<a:spcBef><a:spcPts val="0"/></a:spcBef>'
            '<a:spcAft><a:spcPts val="%d"/></a:spcAft></a:pPr>'
            '<a:r><a:rPr lang="zh-CN" altLang="en-US" sz="%d" b="%s" spc="-50" dirty="0">'
            '<a:solidFill><a:srgbClr val="%s"/></a:solidFill>'
            '<a:latin typeface="%s"/><a:ea typeface="%s"/></a:rPr>'
            '<a:t>%s</a:t></a:r></a:p>'
            % (line_pt, after_pt, int(round(size * 100)), "1" if row.get("bold") else "0",
               _color(row.get("color"), theme), FONT, FONT, escape(row.get("text") or "")))
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="%d" name="文本块%d"/>'
        '<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
        '<p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" anchor="t"/>'
        '<a:lstStyle/>%s</p:txBody></p:sp>'
    ) % (shape_id, shape_id, _emu(first["x_mm"]), _emu(first["y_mm"]),
         _emu(first["width_mm"]), _emu(height), "".join(paragraphs))


def _rect_shape(shape_id, name, x_mm, y_mm, w_mm, h_mm, color):
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '<a:solidFill><a:srgbClr val="%s"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>'
        '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="zh-CN"/></a:p></p:txBody></p:sp>'
    ) % (shape_id, name, _emu(x_mm), _emu(y_mm), _emu(w_mm), _emu(h_mm), color)


def _picture_xml(shape_id, photo, rel_id):
    """证件照：`<p:pic>` 指向 slide rels 里的图片关系（r:embed）。"""
    return (
        '<p:pic><p:nvPicPr><p:cNvPr id="%d" name="证件照"/>'
        '<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
        '<p:blipFill><a:blip r:embed="%s"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        '<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
    ) % (shape_id, rel_id, _emu(photo["x_mm"]), _emu(photo["y_mm"]),
         _emu(photo["width_mm"]), _emu(photo["height_mm"]))


def _slide_xml(page, theme, plan, with_photo=False):
    shapes = []
    shape_id = 2
    # 首页才有抬头色块
    has_band = any(row.get("y_mm") == planner.NAME_Y_MM for row in page["rows"])
    if has_band:
        shapes.append(_rect_shape(shape_id, "抬头色块", 0, 0,
                                  plan["page_w_mm"], plan["band_h_mm"],
                                  _color(theme["header_bg"], theme)))
        shape_id += 1
    row_index = 0
    line_names = {"rule": "板块分隔线", "divider": "分栏竖线", "band_rule": "抬头下沿线"}
    group = []

    def flush():
        """把攒着的一串内容行合成一个文本框（板块 = 标题 + 各条目）。"""
        nonlocal shape_id, group
        if group:
            shapes.append(_text_box(shape_id, group, theme, plan))
            shape_id += 1
            group = []

    for row in page["rows"]:
        row_index += 1
        kind = row.get("kind") or ("rule" if row.get("rule") else "")
        if kind in line_names:
            flush()
            # 板块标题下的线是**细线**：行高里含了与下文的间距，画出来只取线粗
            height = planner.SIZE_RULE if kind == "rule" else row.get("height_mm")
            shapes.append(_rect_shape(shape_id, line_names[kind], row["x_mm"], row["y_mm"],
                                      row["width_mm"], height, _color(row.get("color"), theme)))
            shape_id += 1
        elif row.get("fixed"):
            # 抬头那几行是**绝对定位**的（不能和正文一起往下流，否则就又重叠了）
            flush()
            shapes.append(_text_box(shape_id, [row], theme, plan))
            shape_id += 1
        elif row.get("text"):
            # ⚠️ 两处必须断开分组（2026-10-11 连踩两次）：
            # ① **换栏**：左栏最后一块与右栏第一块紧邻，不断开就会并成一个框、x 取左栏
            #    → 表现「部队经历的题头跑到左栏最下面」；
            # ② **新板块的题头**：同栏内下一块的题头会并进上一块的框尾
            #    → 表现「项目经历/实习/在校的题头不见了」。
            if group and (abs(row["x_mm"] - group[0]["x_mm"]) > 0.01 or row.get("title")):
                flush()
            group.append(row)
    flush()
    # 证件照画在抬头色块之上（放在最后 = 最上层）
    if with_photo and plan.get("photo"):
        shapes.append(_picture_xml(shape_id, plan["photo"], "rId2"))
        shape_id += 1
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        + "".join(shapes) +
        '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'
    )


def _slide_rels(with_photo=False):
    photo_rel = ""
    if with_photo:
        # ⚠️ Target 是相对**源 slide 部件**解析的（不是相对 _rels 目录）→ 一定要写 ../media/…
        # 外部评审 点过名：这里写成 media/… 或 ../media 都会导致 PowerPoint 报"需要修复"。
        photo_rel = ('<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
                     'officeDocument/2006/relationships/image" '
                     'Target="../media/resume-photo.jpg"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        + photo_rel +
        '</Relationships>'
    )


def _presentation_xml(original, page_count):
    """换掉 sldIdLst、删掉 notesMasterIdLst，其余（sldSz 等）原样保留。"""
    text = original.decode("utf-8")
    sld_ids = "".join('<p:sldId id="%d" r:id="rId%d"/>' % (255 + index, 2 + index)
                      for index in range(1, page_count + 1))
    text = re.sub(r"<p:sldIdLst>.*?</p:sldIdLst>",
                  "<p:sldIdLst>%s</p:sldIdLst>" % sld_ids, text, flags=re.S)
    text = re.sub(r"<p:notesMasterIdLst>.*?</p:notesMasterIdLst>", "", text, flags=re.S)
    return text


def _presentation_rels(page_count):
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>',
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/theme" Target="theme/theme1.xml"/>',
    ]
    for index in range(1, page_count + 1):
        lines.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                     'officeDocument/2006/relationships/slide" Target="slides/slide%d.xml"/>'
                     % (2 + index, index))
    base = 2 + page_count
    for offset, (kind, target) in enumerate((("presProps", "presProps.xml"),
                                             ("viewProps", "viewProps.xml"),
                                             ("tableStyles", "tableStyles.xml"))):
        lines.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                     'officeDocument/2006/relationships/%s" Target="%s"/>'
                     % (base + 1 + offset, kind, target))
    lines.append("</Relationships>")
    return "".join(lines)


def _content_types(original, page_count, with_photo=False):
    """保留模板里的 Default/Override，只重建「幻灯片」相关的 Override，并去掉批注部件。"""
    text = original.decode("utf-8")
    text = re.sub(r'<Override PartName="/ppt/notesSlides/[^"]*"[^>]*/>', "", text)
    text = re.sub(r'<Override PartName="/ppt/notesMasters/[^"]*"[^>]*/>', "", text)
    text = re.sub(r'<Override PartName="/ppt/slides/slide\d+\.xml"[^>]*/>', "", text)
    overrides = "".join(
        '<Override PartName="/ppt/slides/slide%d.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.presentationml.slide+xml"/>' % index
        for index in range(1, page_count + 1))
    text = text.replace("</Types>", overrides + "</Types>")
    if with_photo and 'Extension="jpg"' not in text:
        # ⚠️ 必须是 Extension="jpg" + image/jpeg（**不是** image/jpg），且不能重复登记
        text = text.replace("<Default", '<Default Extension="jpg" ContentType="image/jpeg"/>',
                            1)
    return text


def _app_xml(original, page_count):
    """docProps/app.xml 里的幻灯片数同步一下（纯统计信息，不同步也不会报错）。"""
    text = original.decode("utf-8")
    text = re.sub(r"<Slides>\d+</Slides>", "<Slides>%d</Slides>" % page_count, text)
    text = re.sub(r"<HiddenSlides>\d+</HiddenSlides>", "<HiddenSlides>0</HiddenSlides>", text)
    return text
