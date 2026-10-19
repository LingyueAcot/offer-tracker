# -*- coding: utf-8 -*-
"""
tools/test_resume_pptx.py —— 简历导出 PPTX 的自检（纯逻辑，不用开程序）

查两件事：
  ① `core/resume_pptx.plan()`（纯布局规划）：分页、左右栏、隐藏板块、自定义标题、不越界；
  ② `services/resume_pptx.build()`（生成字节）：zip 完整、XML 可解析、多页登记齐全、
     抬头配色跟着 layout.theme、模板里没有个人信息。

用法：
    python tools\\test_resume_pptx.py            # 结构与内容检查
    python tools\\test_resume_pptx.py <目录>      # 指定工作目录（受限环境用）
"""
import io
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app")
if not os.path.isfile(os.path.join(APP, "server.py")):
    print("[FAIL] 找不到 app/server.py —— 脚本位置推算不对，拒绝继续（免得测错对象）")
    sys.exit(1)
sys.path.insert(0, APP)

from core import resume_pptx as planner  # noqa: E402
from services import resume_pptx as exporter  # noqa: E402

ok = True
total = 0
failed = 0
skipped = 0


def check(label, condition, extra=""):
    global ok, total, failed
    total += 1
    if not condition:
        ok = False
        failed += 1
    print(("[PASS] " if condition else "[FAIL] ") + label + (("  " + str(extra)) if extra else ""))


def skip(label, why):
    global skipped
    skipped += 1
    print("[SKIP] " + label + "  " + why)


CONTENT = {
    "basics": {"name": "自检用姓名", "headline": "材料工程硕士 | 2027届", "phone": "13800000000",
               "email": "test@example.com", "gender": "男", "location": "广州", "political": "中共党员",
               "ethnicity": "汉", "hometown": "示例市", "education_level": "硕士研究生"},
    "education": [{"school": "某某大学", "major": "材料工程", "degree": "硕士",
                   "start": "2024.09", "end": "2027.06", "bullets": ["研究方向：界面蒸发器"]}],
    "skills": [{"text": "熟悉材料合成与表征"}],
    "awards": [{"text": "某某奖学金（2026）"}],
    "work": [{"org": "某某材料科技", "role": "联合培养", "start": "2022.06", "end": "2022.09",
              "bullets": ["参与材料改性研究。"]}],
    "military": [{"org": "某某部队", "role": "战士", "start": "2022.09", "end": "2024.09",
                  "bullets": ["服役期间表现突出。"]}],
    "campus": [{"org": "就业指导中心", "role": "助管组长", "start": "2018.09", "end": "2021.06",
                "bullets": ["协助举办招聘会60余场。"]}],
    "projects": [{"name": "某某基金项目", "role": "研究生", "start": "2024.09", "end": "2027.06",
                  "topic": "薄液膜沸腾耦合微纳包络输运结构",
                  "sections": [{"label": "材料合成", "text": "负责复合多孔水凝胶材料制备。"},
                               {"label": "成果产出", "text": "以第一作者发表一区SCI论文1篇。"}]}],
}
LAYOUT = {"font_scale": 1.0, "line_height": 1.55,
          "theme": {"header_bg": "#3B5F82", "header_text": "#FFFFFF", "accent": "#2F6595"}}
BOTTOM_LIMIT = 289.5          # 页面 297 − 底部留白 8，再给 0.5 容差

# ---------------------------------------------------------------- ① 布局规划
plan1 = planner.plan(CONTENT, LAYOUT)
check("规划器：短简历排成 1 页", len(plan1["pages"]) == 1, "实际 %d 页" % len(plan1["pages"]))
rows1 = plan1["pages"][0]["rows"]
texts1 = [r.get("text", "") for r in rows1]
check("规划器：抬头有姓名", "自检用姓名" in texts1)
check("规划器：联系方式行带电话", any(t.startswith("电话：") for t in texts1))
check("规划器：七个板块标题都在",
      all(name in texts1 for name in ("教育经历", "专业技能", "荣誉奖项", "部队经历",
                                      "项目经历", "实习 / 工作", "在校经历")),
      [t for t in texts1 if t and len(t) < 8])
check("规划器：左栏板块在左栏坐标（x=12.2）",
      all(abs(r["x_mm"] - 12.2) < 0.01 for r in rows1 if r.get("text") == "教育经历"))
check("规划器：右栏板块在右栏坐标（x=78.3）",
      all(abs(r["x_mm"] - 78.3) < 0.01 for r in rows1 if r.get("text") == "项目经历"))
check("规划器：不越界（每行底边 ≤ %.1fmm）" % BOTTOM_LIMIT,
      all(r["y_mm"] + r["height_mm"] <= BOTTOM_LIMIT for r in rows1 if "y_mm" in r),
      max(r["y_mm"] + r["height_mm"] for r in rows1 if "y_mm" in r))
check("规划器：铺满整页（最后一行贴近底部）",
      max(r["y_mm"] + r["height_mm"] for r in rows1 if "y_mm" in r) > 280,
      round(max(r["y_mm"] + r["height_mm"] for r in rows1 if "y_mm" in r), 1))

hidden = planner.plan(CONTENT, dict(LAYOUT, hidden_sections=["military"]))
texts_hidden = [r.get("text", "") for r in hidden["pages"][0]["rows"]]
check("规划器：隐藏的板块不出现", "部队经历" not in texts_hidden)
check("规划器：隐藏板块的条目文字也不出现", not any("某某部队" in t for t in texts_hidden))

renamed = planner.plan(CONTENT, dict(LAYOUT, section_titles={"education": "教育背景"}))
texts_renamed = [r.get("text", "") for r in renamed["pages"][0]["rows"]]
check("规划器：自定义板块标题生效", "教育背景" in texts_renamed and "教育经历" not in texts_renamed)

big = dict(CONTENT)
big["awards"] = [{"text": "荣誉第 %d 项：某某奖学金（20%02d）" % (i, 10 + i % 20)} for i in range(1, 26)]
big["skills"] = [{"text": "技能第 %d 条：熟悉材料合成、表征与性能测试流程，能独立设计实验。" % i}
                 for i in range(1, 15)]
big["campus"] = [{"org": "组织%d" % i, "role": "成员", "start": "2018.09", "end": "2020.06",
                  "bullets": ["负责活动设计与现场执行，累计服务同学五百余人次，获得一致好评。"]}
                 for i in range(1, 9)]
plan2 = planner.plan(big, LAYOUT)
check("规划器：内容装不下时续页（>1 页）", len(plan2["pages"]) >= 2, "实际 %d 页" % len(plan2["pages"]))
check("规划器：续页也一个行都不越界",
      all(r["y_mm"] + r["height_mm"] <= BOTTOM_LIMIT
          for page in plan2["pages"] for r in page["rows"] if "y_mm" in r))
check("规划器：续页有「第 N 页」提示行",
      any("第 2 页" in r.get("text", "") for r in plan2["pages"][1]["rows"]))
check("规划器：空内容也能出一页（不崩）", len(planner.plan({}, LAYOUT)["pages"]) >= 1)


def max_column_gap(page, side):
    """同一栏相邻两行之间的最大空隙（mm）。均分只该在"该栏排完"时生效。"""
    rows = sorted([r for r in page["rows"] if r.get("side") == side], key=lambda r: r["y_mm"])
    worst = 0.0
    for first, second in zip(rows, rows[1:]):
        worst = max(worst, second["y_mm"] - (first["y_mm"] + first["height_mm"]))
    return worst


# ⚠️ 2026-10-11 用户报「间距错乱了」：荣誉奖项被推到第 2 页后，左栏的"铺满均分"
# 把 103mm 的剩余空间塞进了两个板块中间，渲染出来中间裂开一个大洞。
gaps2 = [max_column_gap(page, side) for page in plan2["pages"] for side in ("left", "right")]
check("规划器：有板块留到下一页时**不做均分**（栏内不出现 >8mm 的空洞）",
      max(gaps2) <= 8.0, "最大空隙 %.2fmm" % max(gaps2))
fitted = planner.fit_one_page(CONTENT, dict(LAYOUT, font_scale=1.4))
check("规划器：一页优先自适应**不会把字号调大**，且结果 ≤1 页",
      len(fitted["pages"]) == 1 and fitted["font_scale_used"] <= 1.4,
      "字号 %.3f / %d 页" % (fitted.get("font_scale_used"), len(fitted["pages"])))
medium = dict(CONTENT)
medium["awards"] = [{"text": "荣誉第 %d 项：某某奖学金（20%02d）" % (i, 10 + i % 20)}
                    for i in range(1, 14)]
medium["skills"] = [{"text": "技能第 %d 条：熟悉材料合成、表征与性能测试流程。" % i}
                    for i in range(1, 5)]
fitted_medium = planner.fit_one_page(medium, dict(LAYOUT, font_scale=1.4))
check("规划器：内容偏多时能把 1.4 压回一页（真的会缩）",
      len(fitted_medium["pages"]) == 1 and fitted_medium["font_scale_used"] < 1.4,
      "字号 %.3f / %d 页" % (fitted_medium.get("font_scale_used"), len(fitted_medium["pages"])))
check("规划器：自适应后的每一页都不越界、同栏不重叠",
      all(r["y_mm"] + r["height_mm"] <= BOTTOM_LIMIT
          for page in fitted_medium["pages"] for r in page["rows"] if "y_mm" in r)
      and all(max_column_gap(page, side) >= -0.01
              for page in fitted_medium["pages"] for side in ("left", "right")))
fitted_big = planner.fit_one_page(big, LAYOUT)
check("规划器：内容实在太多时允许续页（自适应不硬压）",
      len(fitted_big["pages"]) >= 1, "%d 页" % len(fitted_big["pages"]))
long_text = planner.plan({"basics": {"name": "长文本"}, "skills": [{"text": "很长的中文" * 90}]}, LAYOUT)
check("规划器：超长单条也不越界",
      all(r["y_mm"] + r["height_mm"] <= BOTTOM_LIMIT
          for page in long_text["pages"] for r in page["rows"] if "y_mm" in r))

# ---------------------------------------------------------------- ② 生成字节
data = exporter.build(CONTENT, LAYOUT)
check("生成：能拿到非空字节", isinstance(data, bytes) and len(data) > 4000, "%d 字节" % len(data))
blob = zipfile.ZipFile(__import__("io").BytesIO(data))
check("生成：zip 完整（testzip 无坏部件）", blob.testzip() is None)
names = set(blob.namelist())
for part in ("[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml",
             "ppt/_rels/presentation.xml.rels", "ppt/slides/slide1.xml",
             "ppt/slides/_rels/slide1.xml.rels", "ppt/slideMasters/slideMaster1.xml",
             "ppt/slideLayouts/slideLayout1.xml", "ppt/theme/theme1.xml"):
    check("生成：包含 %s" % part, part in names)
check("生成：批注/备注部件已清掉（免得留悬空关系）",
      not any("notes" in name for name in names))

parsed = True
for name in names:
    if not name.endswith(".xml") and not name.endswith(".rels"):
        continue
    try:
        ET.fromstring(blob.read(name))
    except ET.ParseError as error:
        parsed = False
        print("      解析失败：%s（%s）" % (name, error))
check("生成：所有 XML/rels 都能解析", parsed)

slide = blob.read("ppt/slides/slide1.xml").decode("utf-8")
check("生成：幻灯片里带姓名", "自检用姓名" in slide)
check("生成：抬头底色用的是 layout.theme 的值", 'val="3B5F82"' in slide)
check("生成：强调色用的是 layout.theme 的值", 'val="2F6595"' in slide)
check("生成：字体显式指定（不靠默认字体）", "微软雅黑" in slide)


def line_colors(slide_xml):
    """把三条装饰线的颜色抓出来：板块分隔线 / 分栏竖线 / 抬头下沿线。"""
    found = {}
    for block in re.findall(r"<p:sp>.*?</p:sp>", slide_xml, re.S):
        name = re.search(r'name="([^"]*)"', block)
        color = re.search(r'<a:srgbClr val="([0-9A-Fa-f]{6})"', block)
        if name and color and name.group(1) in ("板块分隔线", "分栏竖线", "抬头下沿线"):
            found.setdefault(name.group(1), []).append(color.group(1))
    return found


lines = line_colors(slide)
check("规划器：板块分隔线的颜色记号是 rule（淡色），不是 accent",
      all(r.get("color") == "rule" for r in rows1 if r.get("rule")),
      [r.get("color") for r in rows1 if r.get("rule")][:3])
check("规划器：每页都带左右分栏的竖线",
      all(any(r.get("kind") == "divider" for r in page["rows"]) for page in plan1["pages"]))
# ⚠️ 2026-10-11 用户报「原来是淡颜色，现在变成实心颜色，而且两栏之间的线不见了」——
# 就是这两条：板块线误用 accent、分栏线压根没画。用例钉死它们，别再犯。
check("生成：板块标题下的线是**淡色**（不是强调色）",
      bool(lines.get("板块分隔线")) and all(c == planner.RULE_COLOR for c in lines["板块分隔线"]),
      lines.get("板块分隔线"))
check("生成：左右两栏之间有**竖线**且为淡色",
      bool(lines.get("分栏竖线")) and all(c == planner.DIVIDER_COLOR for c in lines["分栏竖线"]),
      lines.get("分栏竖线"))
check("生成：抬头色块下沿有细线", lines.get("抬头下沿线") == [planner.BAND_RULE_COLOR],
      lines.get("抬头下沿线"))
# ⚠️ 2026-10-11 用户报「段落与段落间隔和段落内行间隔相差太大」：
# 根因是文本框内部没写行距（吃 PowerPoint 默认 ≈1.2×），而段间按 1.55× 分配。
text_shapes = len(re.findall(r'txBox="1"', slide))
# 只数"有文字的段落"：矩形（色块/细线）里也有一个空的 <a:p>
paragraphs = len(re.findall(r"<a:p><a:pPr", slide))
line_specs = re.findall(r'<a:lnSpc><a:spcPts val="(\d+)"/></a:lnSpc>', slide)
check("生成：每个文字段落都写了**精确行距**（段内与段间同一套倍数）",
      len(line_specs) == paragraphs and 0 < text_shapes <= paragraphs,
      "%d 个文本框 / %d 段 / %d 条行距" % (text_shapes, paragraphs, len(line_specs)))
check("生成：段前距显式归零、段后距显式写出（条目间距靠它，不靠估算高度）",
      slide.count('<a:spcBef><a:spcPts val="0"/></a:spcBef>') == paragraphs
      and len(re.findall(r'<a:spcAft><a:spcPts val="\d+"/></a:spcAft>', slide)) == paragraphs)
# ⚠️ 2026-10-11 用户报「顶部重叠了」：24pt 的名字按 1.55 行距要 13.8mm，压到了副标题上。
header_rows = [r for r in plan1["pages"][0]["rows"] if r.get("fixed")]
check("规划器：抬头各行用**紧行距**（24pt 名字不会压到副标题）",
      bool(header_rows) and all(abs((r.get("line_height") or 0) - planner.HEADER_LINE) < 0.001
                                for r in header_rows)
      and max(r["size_pt"] for r in header_rows) > 20,
      [r.get("line_height") for r in header_rows])
check("生成：抬头那行的精确行距 = 字号 × 1.1（不是 1.55，否则又重叠）",
      str(int(round(planner.SIZE_NAME * planner.HEADER_LINE * 100))) in line_specs,
      "期望 %d，实际含 %s" % (int(round(planner.SIZE_NAME * planner.HEADER_LINE * 100)),
                              sorted(set(line_specs))[:6]))
# 名字那一行排完必须还在副标题上面（6.4 + 24pt×1.1 = 16.2mm < 17.5mm）
name_bottom_mm = planner.NAME_Y_MM + planner.SIZE_NAME * planner.MM_PER_PT * planner.HEADER_LINE
check("规划器：姓名行的下沿仍在副标题之上（不会重叠）",
      name_bottom_mm < planner.SUB_Y_MM, "%.1fmm < %.1fmm" % (name_bottom_mm, planner.SUB_Y_MM))
check("规划器：一条板块的内容在**同一个文本框**里（不再一行一个框）",
      text_shapes < paragraphs)


def boxes_x_text(slide_xml):
    """从幻灯片里取出每个文本框的 x 与全部文字（用于查"跨栏混装"）。"""
    out = []
    for block in re.findall(r"<p:sp>.*?</p:sp>", slide_xml, re.S):
        if 'txBox="1"' not in block:
            continue
        offset = re.search(r'<a:off x="(-?\d+)"', block)
        texts = re.findall(r"<a:t>(.*?)</a:t>", block, re.S)
        if offset:
            out.append((int(offset.group(1)) / 36000.0, " ".join(texts)))
    return out


# ⚠️ 2026-10-11 用户报「【部队经历】题头在左侧最下」：换栏时没断开分组，
# 左栏最后的板块与右栏第一个板块合并成一个框，框的 x 取左栏 → 题头跑进左栏。
LEFT_TITLES = ("教育经历", "专业技能", "荣誉奖项")
RIGHT_TITLES = ("部队经历", "项目经历", "实习 / 工作", "在校经历")
mismatched = []
for box_x, box_text in boxes_x_text(slide):
    if abs(box_x - 12.2) < 0.5 and any(t in box_text for t in RIGHT_TITLES):
        mismatched.append(("左栏框里有右栏板块", box_x, box_text[:40]))
    if abs(box_x - 78.3) < 0.5 and any(t in box_text for t in LEFT_TITLES):
        mismatched.append(("右栏框里有左栏板块", box_x, box_text[:40]))
check("生成：文本框不跨栏混装（左栏框里不许出现右栏板块，反之亦然）",
      not mismatched, mismatched)
# ⚠️ 另一面：题头必须排在它那个框的**最前面**（并进上一个框的尾部 = 题头"不见了"）
ALL_TITLES = LEFT_TITLES + RIGHT_TITLES
misplaced = []
for box_x, box_text in boxes_x_text(slide):
    for title in ALL_TITLES:
        if title in box_text and not box_text.strip().startswith(title):
            misplaced.append((title, box_x, box_text[:40]))
check("生成：每个板块题头都排在所属文本框的**第一段**（不许并到上一个框尾部）",
      not misplaced, misplaced)
check("生成：七个板块题头各出现一次",
      all(sum(1 for _, text in boxes_x_text(slide) if title in text) == 1
          for title in ALL_TITLES))
# ⚠️ 2026-10-11：抬头信息行的 append 被我留了两份，导出件同一位置出现两个一模一样的文本框。
fixed_info = [(row["y_mm"], row.get("text", "")) for row in plan1["pages"][0]["rows"]
              if row.get("fixed") and row.get("y_mm") in planner.INFO_Y_MM]
check("规划器：抬头三行信息各只出现一次（不许重复 append）",
      len(fixed_info) == len(planner.INFO_Y_MM)
      and len(set(y for y, _ in fixed_info)) == len(planner.INFO_Y_MM),
      fixed_info)
check("生成：抬头信息行的文本框不超过 3 个（同一位置不许两个框）",
      len([text for _, text in boxes_x_text(slide)
           if text and any(text.startswith(label) for label in
                           ("电话", "邮箱", "性别", "现所在地", "政治面貌", "民族", "籍贯", "最高学历"))]) <= 3)

# ---- 抬头右边界的规则（2026-10-11 用户拍板：没照片放宽、有照片避让；溢出先缩字再续排） ----
no_photo = planner.plan(CONTENT, LAYOUT)
photo_content = dict(CONTENT)
photo_content["photo"] = {"mime": "image/jpeg", "data_base64": "x"}
with_photo = planner.plan(photo_content, dict(LAYOUT, show_photo=True))
width_no_photo = planner.header_text_width_mm(False)
width_photo = planner.header_text_width_mm(True)
check("规则：没有照片时抬头文字放宽到整条色块（≈185.6mm）",
      abs(width_no_photo - 185.6) < 0.6, round(width_no_photo, 1))
check("规则：有照片时让出照片框（167.7 − 3 − 12.2 ≈ 152.5mm）",
      abs(width_photo - 152.5) < 0.6, round(width_photo, 1))
check("规则：照片框几何与用户原件一致（167.7/4.8/30.7/40.2）",
      (planner.PHOTO_X_MM, planner.PHOTO_Y_MM, planner.PHOTO_W_MM, planner.PHOTO_H_MM)
      == (167.7, 4.8, 30.7, 40.2))


def info_rows_of(plan):
    return [row for row in plan["pages"][0]["rows"]
            if row.get("fixed") and row.get("y_mm") in planner.INFO_Y_MM and row.get("text")]


over_wide = []
for label, plan in (("无照片", no_photo), ("有照片", with_photo)):
    for row in info_rows_of(plan):
        estimated = planner.text_units(row["text"]) * row["size_pt"] * planner.MM_PER_PT
        if estimated > row["width_mm"] + 0.5:
            over_wide.append((label, row["text"][:16], round(estimated, 1), row["width_mm"]))
check("规则：两种状态下的信息行都在可用宽度内（不越界）", not over_wide, over_wide)
check("规则：有照片时信息行的可用宽度确实变窄了",
      info_rows_of(with_photo)[0]["width_mm"] < info_rows_of(no_photo)[0]["width_mm"])

# 8 条超长信息：先缩字（不低于 8.5pt），剩下的续排到正文顶部 —— **一条都不许丢**
long_pairs = [{"label": "第%d项" % i, "value": "很长的信息内容" * 5} for i in range(1, 9)]
long_content = {"basics": {"name": "张三"}, "info": long_pairs}
long_plan = planner.plan(long_content, {"font_scale": 1.0, "line_height": 1.55})
page1 = long_plan["pages"][0]
header_info = info_rows_of(long_plan)
cont = [row for row in page1["rows"]
        if row.get("fixed") and 47.1 < (row.get("y_mm") or 0) < 50.5 and row.get("text")]
all_text = " ".join([row.get("text", "") for row in header_info + cont])
missing = [pair["value"] for pair in long_pairs if pair["value"] not in all_text]
check("规则：信息太多时缩字到下限（不低于 8.5pt×缩放）",
      bool(header_info) and all(row["size_pt"] >= planner.INFO_SIZE_MIN - 0.01 for row in header_info),
      sorted({row["size_pt"] for row in header_info}))
check("规则：抬头 + 续排**一条信息都不丢**（长内容会落到正文顶部续排）",
      not missing, "缺 %d 条" % len(missing))

# ⚠️ 2026-10-11 外部评审 看图抓到的碰撞：「中共党员」被渲染器折成「中共」+「党员」，
# 折出来的第二行溢到下一个固定 y，和「民族」挤在一起。根因是**单条比一行还宽**时
# 打包没切开、交给渲染器自己折行。现在必须在打包阶段就切开（每行都真的 ≤ 可用宽度）。
huge = {"basics": {"name": "张三"}, "info": [
    {"label": "政治面貌", "value": "中共党员"},
    {"label": "备注", "value": "这是一条特别长的信息" * 8},
]}
huge_plan = planner.plan(huge, {"font_scale": 1.0, "line_height": 1.55})
overflow_rows = []
for row in huge_plan["pages"][0]["rows"]:
    if not row.get("text") or not row.get("fixed"):
        continue
    estimated = planner.text_units(row["text"]) * row["size_pt"] * planner.MM_PER_PT
    if estimated > row["width_mm"] + 0.5:
        overflow_rows.append((row["text"][:16], round(estimated, 1), row["width_mm"]))
check("规则：**超宽的单条信息会被切开**，不留给渲染器自动折行（否则会撞下一行）",
      not overflow_rows, overflow_rows)
# ---- 自定义板块（2026-10-11，MVP 文本型）：进规划层；软删除不输出 ----
_custom = dict(CONTENT)
_custom["custom_sections"] = [{"id": "cs_1", "kind": "text", "deleted": False,
                               "items": [{"text": "志愿服务：社区支教 120 小时"}]}]
_custom_layout = dict(LAYOUT, section_order={"left": ["education"], "right": ["cs_1"]},
                      section_titles={"cs_1": "志愿服务"})
_custom_plan = planner.plan(_custom, _custom_layout)
_rows = _custom_plan["pages"][0]["rows"]
check("自定义板块：标题用 layout 里的名字出现在排版里",
      "志愿服务" in [r.get("text") for r in _rows if r.get("title")],
      [r.get("text") for r in _rows if r.get("title")])
check("自定义板块：条目文本出现在排版里",
      any("社区支教" in (r.get("text") or "") for r in _rows))
check("自定义板块：算在右栏（x 用的是右栏坐标）",
      any(r.get("x_mm", 0) > 70 for r in _rows if "社区支教" in (r.get("text") or "")))
_soft_content = dict(_custom, custom_sections=[dict(_custom["custom_sections"][0], deleted=True)])
_soft_plan = planner.plan(_soft_content, _custom_layout)
check("自定义板块：软删除后不再输出",
      "志愿服务" not in [r.get("text") for r in _soft_plan["pages"][0]["rows"] if r.get("title")])
check("自定义板块：没有标题时退回「自定义板块」",
      "自定义板块" in [r.get("text") for r in planner.plan(_custom, dict(_custom_layout, section_titles={}))["pages"][0]["rows"] if r.get("title")])

check("规则：切开后每行的估算宽度都 ≤ 可用宽度（抬头 + 续排一起查）",
      all(planner.text_units(row["text"]) * row["size_pt"] * planner.MM_PER_PT
          <= row["width_mm"] + 0.5
          for row in huge_plan["pages"][0]["rows"]
          if row.get("text") and row.get("fixed")))

# ---- 证件照（2026-10-11 加）：显示时嵌图；隐藏时**一个字节都不进包** ----
import base64  # noqa: E402

photo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "photo-ok.jpg")
if os.path.isfile(photo_path):
    with open(photo_path, "rb") as handle:
        photo_bytes = handle.read()
    photo_b64 = base64.b64encode(photo_bytes).decode("ascii")

    shown_content = dict(CONTENT)
    shown_content["photo"] = {"mime": "image/jpeg", "data_base64": photo_b64,
                              "width": 458, "height": 600}
    shown_layout = dict(LAYOUT, show_photo=True)
    shown_plan = planner.plan(shown_content, shown_layout)
    hidden_plan = planner.plan(shown_content, dict(LAYOUT, show_photo=False))

    check("证件照：显示时规划层带上照片（框位 167.7/4.8/30.7/40.2）",
          shown_plan.get("photo") and round(shown_plan["photo"]["x_mm"], 1) == 167.7
          and round(shown_plan["photo"]["width_mm"], 1) == 30.7)
    check("证件照：不显示时规划层**完全没有照片键**（按没照片处理）",
          hidden_plan.get("photo") is None)
    check("证件照：显示照片时抬头自动收窄（152.5mm 那套规则）",
          abs(planner.header_text_width_mm(True) - 152.5) < 0.6)

    shown = exporter.build(shown_content, shown_layout)
    hidden = exporter.build(shown_content, dict(LAYOUT, show_photo=False))
    blob_shown = zipfile.ZipFile(io.BytesIO(shown))
    blob_hidden = zipfile.ZipFile(io.BytesIO(hidden))
    names_shown = set(blob_shown.namelist())
    names_hidden = set(blob_hidden.namelist())

    check("证件照：显示时写了 ppt/media/resume-photo.jpg 且字节与原图一致",
          "ppt/media/resume-photo.jpg" in names_shown
          and blob_shown.read("ppt/media/resume-photo.jpg") == photo_bytes)
    check("证件照：隐藏时**包内没有 media 部件**（免得看不见却藏在文件里）",
          not any(name.startswith("ppt/media/") for name in names_hidden))
    rels_shown = blob_shown.read("ppt/slides/_rels/slide1.xml.rels").decode("utf-8")
    rels_hidden = blob_hidden.read("ppt/slides/_rels/slide1.xml.rels").decode("utf-8")
    check("证件照：图片关系的 Target 是 ../media/resume-photo.jpg（相对 slide 部件解析）",
          'Target="../media/resume-photo.jpg"' in rels_shown
          and 'relationships/image' in rels_shown)
    check("证件照：隐藏时 slide rels 里**没有图片关系**（不留悬空关系）",
          "relationships/image" not in rels_hidden)
    types_shown = blob_shown.read("[Content_Types].xml").decode("utf-8")
    types_hidden = blob_hidden.read("[Content_Types].xml").decode("utf-8")
    check("证件照：Content_Types 里是 Extension=\"jpg\" + image/jpeg（不是 image/jpg）",
          'Extension="jpg"' in types_shown and 'ContentType="image/jpeg"' in types_shown
          and 'image/jpg' not in types_shown)
    check("证件照：隐藏时不登记 jpg 类型（不留下无用登记）",
          'Extension="jpg"' not in types_hidden)
    slide_shown = blob_shown.read("ppt/slides/slide1.xml").decode("utf-8")
    slide_hidden = blob_hidden.read("ppt/slides/slide1.xml").decode("utf-8")
    check("证件照：幻灯片里有 <p:pic> 且 r:embed 指向 rId2",
          "<p:pic>" in slide_shown and 'r:embed="rId2"' in slide_shown)
    check("证件照：隐藏时幻灯片里**没有** <p:pic>", "<p:pic>" not in slide_hidden)
    # 1mm = 36000 EMU：167.7→6037200、4.8→172800、30.7→1105200、40.2→1447200
    check("证件照：位置与尺寸换算成 EMU 正确（167.7/4.8/30.7/40.2 mm）",
          'x="6037200"' in slide_shown and 'y="172800"' in slide_shown
          and 'cx="1105200"' in slide_shown and 'cy="1447200"' in slide_shown,
          "EMU 见断言")
    check("证件照：形状 id 仍然唯一（加了 <p:pic> 之后）",
          len(re.findall(r'<p:cNvPr id="(\d+)"', slide_shown))
          == len(set(re.findall(r'<p:cNvPr id="(\d+)"', slide_shown))))
    parsed_ok = True
    for name in names_shown:
        if not (name.endswith(".xml") or name.endswith(".rels")):
            continue
        try:
            ET.fromstring(blob_shown.read(name))
        except ET.ParseError:
            parsed_ok = False
    check("证件照：显示照片时 XML 仍全部可解析", parsed_ok)
else:
    print("[SKIP] 证件照用例：找不到 tools/fixtures/photo-ok.jpg")
info_width_limit = plan1["page_w_mm"] - planner.LEFT_X_MM * 2 + 0.5
too_wide = []
for row in plan1["pages"][0]["rows"]:
    if not row.get("fixed") or row.get("y_mm") not in planner.INFO_Y_MM or not row.get("text"):
        continue
    estimated = planner.text_units(row["text"]) * row["size_pt"] * planner.MM_PER_PT
    if estimated > info_width_limit:
        too_wide.append((row["text"][:18], round(estimated, 1)))
check("规划器：信息行按估算宽度打包（每行都不越界）", not too_wide, too_wide)
check("规划器：条目额外间距只有行距的一小部分（不至于看起来空一行）",
      planner.GAP_ITEM_MM <= 0.45 * (planner.SIZE_BODY * planner.MM_PER_PT * 1.55),
      "条目额外 %.2fmm vs 一行 %.2fmm" % (planner.GAP_ITEM_MM,
                                          planner.SIZE_BODY * planner.MM_PER_PT * 1.55))
check("规划器：行距下限生效（避免精确行距把字切掉）",
      planner.effective_line_height(0.8) == planner.LINE_MIN
      and planner.effective_line_height(1.55) == 1.55)
check("生成：形状 id 唯一",
      len(re.findall(r'<p:cNvPr id="(\d+)"', slide)) == len(set(re.findall(r'<p:cNvPr id="(\d+)"', slide))))
escaped = zipfile.ZipFile(io.BytesIO(
    exporter.build({"basics": {"name": "A&B<C>"}}, LAYOUT))).read("ppt/slides/slide1.xml").decode("utf-8")
check("生成：特殊字符被转义（& < > 不破坏 XML）",
      "&amp;" in escaped and "&lt;" in escaped, escaped[escaped.find("A&amp;"):][:24] if "A&amp;" in escaped else "没找到")

multi = exporter.build(big, LAYOUT)
blob2 = zipfile.ZipFile(__import__("io").BytesIO(multi))
names2 = set(blob2.namelist())
pages2 = len(plan2["pages"])
check("多页：每页都有 slideN.xml",
      all(("ppt/slides/slide%d.xml" % i) in names2 for i in range(1, pages2 + 1)),
      "应 %d 页" % pages2)
check("多页：每页都有对应的 rels",
      all(("ppt/slides/_rels/slide%d.xml.rels" % i) in names2 for i in range(1, pages2 + 1)))
content_types = blob2.read("[Content_Types].xml").decode("utf-8")
check("多页：[Content_Types].xml 里每页都登记了",
      all(('/ppt/slides/slide%d.xml' % i) in content_types for i in range(1, pages2 + 1)))
presentation = blob2.read("ppt/presentation.xml").decode("utf-8")
check("多页：presentation.xml 的 sldIdLst 有 %d 条" % pages2,
      len(re.findall(r"<p:sldId ", presentation)) == pages2)
check("多页：不再引用 notesMaster（已删掉那个部件）", "notesMaster" not in presentation)
rels = blob2.read("ppt/_rels/presentation.xml.rels").decode("utf-8")
check("多页：rels 里每页都有关系",
      all(('slides/slide%d.xml' % i) in rels for i in range(1, pages2 + 1)))
check("多页：rels 里没有悬空指向 notesMaster", "notesMaster" not in rels)
check("多页：python-pptx 之外的独立校验 —— 每页尺寸仍由 sldSz 决定（A4 纵向）",
      'cx="7562850"' in presentation and 'cy="10696575"' in presentation)

# ---------------------------------------------------------------- ③ 模板隐私
template = exporter.template_path()
check("模板：内置模板文件在", os.path.isfile(template), template)
if os.path.isfile(template):
    raw = zipfile.ZipFile(template)
    residue = []
    for name in raw.namelist():
        if name.endswith("/"):
            continue
        text = raw.read(name).decode("utf-8", "ignore")
        for needle in ("张小明", "138-0000-0000", "zhangxiaoming", "广东工业大学", "示例市"):
            if needle in text:
                residue.append((name, needle))
    check("模板：不含用户个人信息（发布包会带上它）", not residue, residue)
    check("模板：只有 1 张幻灯片（多页靠生成）",
          len([n for n in raw.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)]) == 1)

print("")
print("共 %d 项：通过 %d · 失败 %d · 跳过 %d" % (total, total - failed - skipped, failed, skipped))
print("结果：全部通过" if ok else "结果：存在失败项")
sys.exit(0 if ok else 1)
