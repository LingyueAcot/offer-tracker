# -*- coding: utf-8 -*-
"""
tools/test_resume.py —— 简历域（core/resume.py + services/resumes.py）回归测试

不用开程序、不碰真实数据：全部在**自造的临时库**上跑。
（2026-10-10 加，配合「简历编辑 + PDF 导出」）

跑法：
    python tools\\test_resume.py              # 在系统临时目录里造试验场
    python tools\\test_resume.py <空目录>      # 指定试验场

覆盖方案第 5 步的验收点：
  新增 / 读取 / 保存 / 删除都能在临时库完成；写操作记审计；
  非法输入返回明确错误且**不改库**；过期版本保存**拒绝覆盖**；
  复制与「设为默认」的语义；`build_state()` 里带简历元信息。
"""
import base64
import io
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

import schema      # noqa: E402
import server      # noqa: E402
import store       # noqa: E402
from core import resume as resume_core           # noqa: E402
from services import audit                      # noqa: E402
from services import resumes as resumes_service  # noqa: E402
from services.state import build_state           # noqa: E402

fails = []
passed = []


def check(label, cond, extra=""):
    print(("[PASS] " if cond else "[FAIL] ") + label + (("  " + str(extra)) if extra else ""))
    (passed if cond else fails).append(label)


def raw_row(resume_id):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


scratch = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="resume_test_")
os.makedirs(scratch, exist_ok=True)
DB = os.path.join(scratch, "applications.db")
CHANGELOG = os.path.join(scratch, "change_log.jsonl")

# 造一个空库（走项目自己的 schema.py，不自己写 DDL —— 结构只许那一个地方定义）
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
schema.ensure_schema(conn, server.PRESET_STAGES, server.SCHEMA_VERSION, server.APP_VERSION)
conn.commit()
conn.close()

store.use(DB)
audit.use(CHANGELOG)

# ---------- 1) 空库 ----------
check("空库：简历列表为空", resumes_service.list_resumes() == [])
check("空库：取不存在的简历返回 None", resumes_service.get_resume(1) is None)

# ---------- 2) 新增 ----------
first_id, error = resumes_service.create_resume({"title": " 我的简历 "})
check("新增成功并返回 id", first_id and error is None, "%s / %s" % (first_id, error))
row = raw_row(first_id)
check("新增：标题去了首尾空白", row and row["title"] == "我的简历", row and row["title"])
check("新增：第一份自动成为默认", row and row["is_default"] == 1)
check("新增：rev 从 1 起、content_ver 落库",
      row and row["rev"] == 1 and row["content_ver"] == resume_core.CONTENT_VER,
      row["content_ver"] if row else None)

second_id, error = resumes_service.create_resume({"title": "第二份"})
check("第二份不抢默认", raw_row(second_id)["is_default"] == 0)

# ---------- 3) 读取 ----------
full = resumes_service.get_resume(first_id)
check("读取：content 七个板块齐",
      full and all(k in full["content"] for k in
                   ("basics", "education", "work", "military", "campus", "projects", "skills", "awards")))
check("读取：layout 有模板名与两栏顺序",
      full and full["layout"]["template"] == "two-column-private"
      and full["layout"]["section_order"]["left"][0] == "education")
listed = resumes_service.list_resumes()
check("列表：共两份、默认那份排最前",
      len(listed) == 2 and listed[0]["id"] == first_id, [i["title"] for i in listed])
check("列表：不带 content（省流量）", listed and "content" not in listed[0])

# ---------- 4) 保存 ----------
payload = {
    "rev": 1,
    "title": "我的简历（改过）",
    "content": {
        "basics": {"name": "张三", "phone": "138"},
        "education": [{"school": "广东工业大学", "major": "材料", "bullets": ["一", "二"]}],
        "skills": ["技能一"],
    },
    "layout": {"font_scale": 1.1},
}
error, conflict = resumes_service.save_resume(first_id, payload)
check("保存成功且无冲突", error is None and conflict is None, "%s / %s" % (error, conflict))
row = raw_row(first_id)
check("保存：rev 递增到 2", row["rev"] == 2, row and row["rev"])
saved = resumes_service.get_resume(first_id)
check("保存：正文写进去了", saved["content"]["basics"]["name"] == "张三")
check("保存：教育条目拿到稳定 id", saved["content"]["education"][0]["id"] == "edu_1")
check("保存：layout 与正文分开存（font_scale 生效、模板名保留）",
      saved["layout"]["font_scale"] == 1.1 and saved["layout"]["template"] == "two-column-private")

# ---------- 5) 过期版本 → 拒绝覆盖 ----------
stale = dict(payload)
stale["rev"] = 1                      # 拿旧版本号再存一次
stale["content"] = {"basics": {"name": "不该被写进去"}}
error, conflict = resumes_service.save_resume(first_id, stale)
check("过期 rev：不报错误、返回冲突信息", error is None and isinstance(conflict, dict), "%s / %s" % (error, conflict))
check("过期 rev：冲突信息带服务端当前 rev", conflict and conflict.get("rev") == 2, conflict)
check("过期 rev：库里的内容一个字都没变",
      raw_row(first_id)["rev"] == 2 and resumes_service.get_resume(first_id)["content"]["basics"]["name"] == "张三")
check("过期 rev：正文也没被那次的脏内容污染",
      "不该被写进去" not in (raw_row(first_id)["content"] or ""))

# ---------- 6) 非法输入 → 明确错误且不改库 ----------
error, conflict = resumes_service.save_resume(first_id, {"content": {"basics": {}}})
check("缺 rev → 明确报错", error is not None and "rev" in error, error)

error, conflict = resumes_service.save_resume(first_id, {"rev": 2, "content": "不是对象"})
check("content 类型错 → 明确报错", error is not None and "必须是对象" in error, error)
check("非法输入后 rev 与内容均未变",
      raw_row(first_id)["rev"] == 2 and resumes_service.get_resume(first_id)["content"]["basics"]["name"] == "张三")

error, conflict = resumes_service.save_resume(999999, {"rev": 1})
check("保存不存在的简历 → 报错", error == "简历不存在", error)

# ---------- 7) 复制 ----------
copy_id, error = resumes_service.duplicate_resume(first_id)
check("复制成功", copy_id and error is None, error)
copied = resumes_service.get_resume(copy_id)
check("复制：正文内容一致（深拷贝）", copied["content"]["basics"]["name"] == "张三")
check("复制：标题带「（副本）」", copied["title"].endswith("（副本）"), copied["title"])
check("复制：rev 重置为 1、不是默认", raw_row(copy_id)["rev"] == 1 and raw_row(copy_id)["is_default"] == 0)
c = copied["content"]
c["basics"]["name"] = "改了副本"
resumes_service.save_resume(copy_id, {"rev": 1, "content": c})
check("复制是深拷贝：改副本不影响原件",
      resumes_service.get_resume(first_id)["content"]["basics"]["name"] == "张三")

# ---------- 8) 设为默认 ----------
error = resumes_service.set_default_resume(second_id)
check("设为默认成功", error is None, error)
defaults = [i["id"] for i in resumes_service.list_resumes() if i["is_default"]]
check("同时只有一份默认", defaults == [second_id], defaults)

# ---------- 9) 删除（删掉默认那份 → 自动顺延） ----------
error = resumes_service.delete_resume(second_id)
check("删除成功", error is None, error)
check("删除后取不到", resumes_service.get_resume(second_id) is None)
after = resumes_service.list_resumes()
check("删掉默认那份后，剩下第一份被提为默认",
      len(after) == 2 and sum(i["is_default"] for i in after) == 1,
      [(i["id"], i["is_default"]) for i in after])
check("删除不存在的 → 报错", resumes_service.delete_resume(999999) == "简历不存在")

# ---------- 10) 审计 ----------
entries = []
if os.path.isfile(CHANGELOG):
    with io.open(CHANGELOG, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
actions = [(e["action"], e["entity"]) for e in entries]
check("审计：新增/保存/复制/删除都记了", ("create", "resume") in actions and ("update", "resume") in actions
      and ("delete", "resume") in actions, actions[:6])
check("审计：删除记录带删除前内容", any(e["action"] == "delete" and e["before"] for e in entries))

# ---------- 11) build_state 带简历元信息 ----------
state = build_state()
check("build_state 里有 resumes", "resumes" in state and isinstance(state["resumes"], list))
check("build_state 里的简历只有元信息（不带 content）",
      state["resumes"] and "content" not in state["resumes"][0], list(state["resumes"][0].keys())[:6])

# ---------- 12) 数据包往返（数据包补齐：导出必须带正文，老包不许清空简历） ----------
exported = resumes_service.export_all()
check("export_all 带正文（不是元信息空壳）",
      exported and exported[0]["content"]["basics"]["name"] == "张三",
      (exported[0].get("content", {}).get("basics", {}) if exported else None))

count_before = len(resumes_service.list_resumes())
error = server.import_package({"applications": []})
check("导入「没有 resumes 键」的老数据包：不报错", error is None, error)
check("老数据包**不清空**简历", len(resumes_service.list_resumes()) == count_before,
      "%d → %d" % (count_before, len(resumes_service.list_resumes())))

pkg = {"applications": [], "resumes": [
    {"title": "包里的简历", "is_default": True,
     "content": {"basics": {"name": "李四"}, "education": [{"school": "某大学"}]},
     "layout": {"font_scale": 1.1}},
]}
error = server.import_package(pkg)
check("导入带 resumes 的数据包：不报错", error is None, error)
rows = resumes_service.list_resumes()
check("包里有简历时按 replace 语义替换（只剩包里那份、且是默认）",
      len(rows) == 1 and rows[0]["title"] == "包里的简历" and rows[0]["is_default"] == 1,
      [(r["title"], r["is_default"]) for r in rows])
restored = resumes_service.get_resume(rows[0]["id"])
check("导入的正文与排版都对",
      restored["content"]["basics"]["name"] == "李四"
      and restored["content"]["education"][0]["id"] == "edu_1"
      and restored["layout"]["font_scale"] == 1.1)

# ---------- 13) 基本信息行（2026-10-11 用户要求：8 行「标签：内容」可改可删可加，上限 8） ----------
defaults = resume_core.default_content()
check("默认正文带 8 行基本信息（默认标签齐全）",
      len(defaults["info"]) == resume_core.MAX_INFO_LINES
      and [r["label"] for r in defaults["info"]][:3] == ["电话", "邮箱", "性别"],
      len(defaults["info"]))

# 老数据（只有 basics 的 8 个字段、没有 info）要能自动迁移
legacy = {"basics": {"name": "老王", "phone": "138", "email": "a@b.c", "gender": "男",
                     "location": "广州", "political": "群众", "ethnicity": "汉",
                     "hometown": "示例市", "education_level": "本科"}}
migrated, error = resume_core.normalize_content(legacy)
check("老数据自动迁移成 info 行（标签用默认的、值不丢）", error is None
      and [r["label"] for r in migrated["info"]] == [label for label, _ in resume_core.DEFAULT_INFO]
      and migrated["info"][0]["value"] == "138" and migrated["info"][1]["value"] == "a@b.c",
      error)
check("迁移后 info 行的 id 是 info_1..info_8",
      [r["id"] for r in migrated["info"]] == ["info_%d" % i for i in range(1, 9)])
check("兼容：默认标签的值仍写回老字段（老读法还能取到电话邮箱）",
      migrated["basics"]["phone"] == "138" and migrated["basics"]["email"] == "a@b.c")

custom, error = resume_core.normalize_content({"info": [
    {"label": "身高", "value": "175cm"}, {"label": "驾照", "value": "C1"},
]})
check("自定义标签与内容能存下来", error is None and custom["info"][0]["label"] == "身高"
      and custom["info"][1]["value"] == "C1", error)

over, error = resume_core.normalize_content({"info": [
    {"label": "第%d条" % i, "value": "v%d" % i} for i in range(1, 12)]})
check("超过 8 条只保留前 8 条（不报错、不炸）",
      error is None and len(over["info"]) == resume_core.MAX_INFO_LINES, len(over["info"]))

empty_dropped, error = resume_core.normalize_content({"info": [
    {"label": "", "value": ""}, {"label": "电话", "value": "139"}, {"label": "备用", "value": ""},
]})
check("全空的行被丢掉、只填标签的行保留（标签可空）",
      error is None and len(empty_dropped["info"]) == 2
      and empty_dropped["info"][1]["label"] == "备用", empty_dropped["info"])

long_label, error = resume_core.normalize_content({"info": [{"label": "很长的标签" * 5, "value": "x"}]})
check("标签超长会被截断（不会把版面撑坏）",
      error is None and len(long_label["info"][0]["label"]) <= resume_core.MAX_INFO_LABEL,
      long_label["info"][0]["label"])

all_deleted, error = resume_core.normalize_content({"info": []})
check("8 行全删光也行（info 空数组，不回落成默认 8 行）",
      error is None and all_deleted["info"] == [])

# ---------- 14) 数据包带照片（用户拍板：主动导出的数据包要带照片） ----------
_photo_path = os.path.join(ROOT, "tools", "fixtures", "photo-ok.jpg")
if os.path.isfile(_photo_path):
    with open(_photo_path, "rb") as _handle:
        _photo_bytes = _handle.read()
    _photo_b64 = base64.b64encode(_photo_bytes).decode("ascii")
    _created, _error = resumes_service.create_resume({
        "title": "带照片的简历",
        "content": {"basics": {"name": "带照片"}, "info": [],
                    "photo": {"mime": "image/jpeg", "data_base64": _photo_b64}}})
    check("数据包里的证件照：能存进库（走 core 校验）", _error is None, _error)
    _one = resumes_service.get_resume(_created)
    check("库里取回的证件照尺寸/字节都对",
          _one["content"]["photo"]["width"] == 458
          and base64.b64decode(_one["content"]["photo"]["data_base64"]) == _photo_bytes)
    _pkg = server.export_package()
    _in_pkg = [r for r in _pkg.get("resumes", []) if r.get("title") == "带照片的简历"]
    check("导出的数据包里**带上了照片**（用户拍板要带）",
          bool(_in_pkg) and _in_pkg[0]["content"]["photo"]["data_base64"] == _photo_b64)
    _restored, _error = resume_core.normalize_content(_in_pkg[0]["content"])   # 取"带照片的简历"那份
    check("数据包里的照片能原样导回来（往返字节一致）",
          _error is None and base64.b64decode(_restored["photo"]["data_base64"]) == _photo_bytes,
          _error)
    resumes_service.delete_resume(_created)
else:
    print("[SKIP] 数据包证件照用例：找不到 tools/fixtures/photo-ok.jpg")

# ---------- 15) 自定义板块（凭空新增板块，MVP：文本型 + 软删除 + 上限） ----------
check("自定义板块：新简历里是空数组", resume_core.default_content()["custom_sections"] == [])
_one, _err = resume_core.normalize_content(
    {"custom_sections": [{"id": "cs_1", "items": [{"text": "志愿服务"}]}]})
check("自定义板块：id / kind / 条目都归一化正确",
      _err is None and _one["custom_sections"][0]["id"] == "cs_1"
      and _one["custom_sections"][0]["kind"] == "text"
      and _one["custom_sections"][0]["items"] == [{"text": "志愿服务"}], _err)
check("自定义板块：归一化幂等（id 不重生、内容不变）",
      resume_core.normalize_content(_one)[0]["custom_sections"] == _one["custom_sections"])
_bad = resume_core.normalize_content({"custom_sections": [{"id": "中文 id!", "items": [{"text": "x"}]}]})[0]
check("自定义板块：非法 id 换成 cs_N 形状", _bad["custom_sections"][0]["id"] == "cs_1",
      _bad["custom_sections"][0]["id"])
_soft = resume_core.normalize_content(
    {"custom_sections": [{"id": "cs_1", "deleted": True, "items": [{"text": "旧内容"}]}]})[0]
check("自定义板块：软删除保留数据（不静默丢）",
      _soft["custom_sections"][0]["deleted"] is True
      and _soft["custom_sections"][0]["items"] == [{"text": "旧内容"}])
_many = {"custom_sections": [{"id": "cs_%d" % i, "items": [{"text": "t"}]} for i in range(1, 10)]}
check("自定义板块：超过 8 个时写库被拒", resume_core.custom_section_error(_many) is not None,
      resume_core.custom_section_error(_many))
_cut = resume_core.normalize_content(_many)[0]["custom_sections"]
check("自定义板块：归一化时超出上限的标记软删除、记录一条不丢",
      sum(1 for e in _cut if not e["deleted"]) == 8 and len(_cut) == 9)
_lay, _ = resume_core.normalize_layout({"section_order": {"right": ["cs_1"]},
                                        "section_titles": {"cs_1": "志愿服务"}})
check("自定义板块：排版里的自定义键不被丢掉",
      _lay["section_order"]["right"] == ["cs_1"] and _lay["section_titles"].get("cs_1") == "志愿服务")
_junk, _ = resume_core.normalize_layout({"section_order": {"left": ["education", "这不是键"]}})
check("自定义板块：乱七八糟的键仍然被丢掉", _junk["section_order"]["left"] == ["education"])

# ---------- 16) 自定义板块：数据包往返（外部评审方案里的验收项）----------
_cid, _cerr = resumes_service.create_resume({
    "title": "带自定义板块",
    "content": {"basics": {"name": "带板块"}, "info": [],
                "custom_sections": [{"id": "cs_1", "items": [{"text": "志愿服务 A"}]}]},
    "layout": {"section_titles": {"cs_1": "志愿服务"},
               "section_order": {"left": [], "right": ["cs_1"]}}})
check("数据包：能存下自定义板块（走真实接口）", _cerr is None, _cerr)
_pkg2 = server.export_package()
_in2 = [r for r in _pkg2.get("resumes", []) if r.get("title") == "带自定义板块"]
check("数据包：导出的包里带上了自定义板块",
      bool(_in2) and _in2[0]["content"]["custom_sections"][0]["items"][0]["text"] == "志愿服务 A")
check("数据包：板块标题（在 layout 里）也带上了",
      bool(_in2) and _in2[0]["layout"]["section_titles"].get("cs_1") == "志愿服务")
_restored2, _err2 = resume_core.normalize_content(_in2[0]["content"])
check("数据包：自定义板块能原样导回（id / 文本一致）",
      _err2 is None and _restored2["custom_sections"][0]["id"] == "cs_1"
      and _restored2["custom_sections"][0]["items"][0]["text"] == "志愿服务 A", _err2)
if _cid:
    resumes_service.delete_resume(_cid)

print("")
print("共 %d 项：通过 %d · 失败 %d" % (len(passed) + len(fails), len(passed), len(fails)))
print("结果：%s" % ("全部通过" if not fails else "存在失败项"))
if fails:
    print("失败项：" + "；".join(fails))
sys.exit(0 if not fails else 1)
