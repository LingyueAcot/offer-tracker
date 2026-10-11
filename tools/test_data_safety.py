# -*- coding: utf-8 -*-
"""
数据红线检查：任何操作都不许碰 data/。

两种用法：
  1) python tools\test_data_safety.py
     静态检查：包/升级脚本里有没有可能碰到数据、.gitignore 有没有排除数据、
     隐私自查还在不在。退出码 0/1。
  2) python tools\test_data_safety.py --snapshot     （拍指纹，动手之前）
     ...随便跑什么（升级、迁移、试功能）...
     python tools\test_data_safety.py --compare      （对指纹，动手之后）
     指纹不一致就报错并列出是哪个文件变了。这两个模式配合使用，
     等于给"不碰数据"这条原则装了一个可执行的证据。

红线（写进 AGENTS.md 的第一条原则）：
  · app/data/ 是唯一一份真实数据，程序之外的东西一律只读；
  · 升级只换程序文件，data/ logs/ config.json startup-log.txt 永不进入替换范围；
  · 自检、迁移、试新功能一律拿副本做，绝不用真库当试验田；
  · 每次启动的备份（backup.py）不自动清理。
"""
from __future__ import print_function

import hashlib
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
DATA = os.path.join(APP, "data")
FINGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), u".scratch",
                      u"_数据指纹.json")

# 绝不能出现在任何发布包/升级负载里的东西
FORBIDDEN_IN_PAYLOAD = ["applications.db", "change_log.jsonl",
                        u"个人画像.json", "window_state.json", "runtime.json"]
# 升级脚本必须显式排除的名字（用户自己的文件）
MUST_KEEP = ["config.json", "startup-log.txt", "data", "logs",
             u"个人画像.json", "webview_profile"]

results = []


def check(name, ok, detail=u""):
    results.append(bool(ok))
    print(u"  [%s] %s%s" % (u"PASS" if ok else u"FAIL", name,
                            (u" -> " + detail) if detail else u""))


def read(path):
    try:
        return io.open(path, encoding="utf-8", errors="ignore").read()
    except IOError:
        return u""


def payload_names(pkg_dir):
    names = []
    for dirpath, _dirs, files in os.walk(pkg_dir):
        for name in files:
            names.append(name)
    return names


def static_checks():
    print(u"静态检查：")
    gi = read(os.path.join(ROOT, ".gitignore"))
    check(u".gitignore 排除 app/data/ 与 app/logs/",
          "app/data/" in gi and "app/logs/" in gi)
    check(u".gitignore 排除试用包/（发布产物不进仓库）", u"试用包/" in gi)

    maker = read(os.path.join(ROOT, "tools", "make_trial_package.py"))
    check(u"打包器带隐私自查（查数据文件与这台电脑路径）",
          "applications.db" in maker and u"个人画像.json" in maker
          and u"张小明" in maker)
    check(u"打包器的负载清单里没有 data/ 与 logs/",
          '"data"' not in maker.split("DIRS = ")[1].split("\n")[0]
          and '"logs"' not in maker.split("DIRS = ")[1].split("\n")[0])

    up = read(os.path.join(ROOT, "tools", "make_trial_package.py"))
    for name in ("config.json", "startup-log.txt"):
        needle = 'not "' + "%%F" + '"=="' + name + '"'
        check(u"升级脚本显式排除 %s" % name, needle in up)

    ps1 = read(os.path.join(ROOT, "tools", "one-click-upgrade.ps1"))
    if ps1:
        missing = [n for n in ("config.json", "startup-log.txt", "data", "logs")
                   if n not in ps1]
        check(u"一键升级的排除清单完整", not missing,
              u"缺：%s" % u"、".join(missing) if missing else u"")
    else:
        check(u"一键升级脚本存在", False, u"没找到 one-click-upgrade.ps1")

    # 发布目录里已有的包，逐个看内容（有才查，没有就跳过）
    for pkg, label in ((os.path.join(ROOT, u"试用包", u"应届海投管理-试用版"), u"试用包目录"),
                       (os.path.join(ROOT, u"试用包", u"应届海投管理-升级包"), u"升级包目录")):
        if not os.path.isdir(pkg):
            continue
        bad = [n for n in payload_names(pkg) if n in FORBIDDEN_IN_PAYLOAD]
        check(u"%s 里没有数据文件" % label, not bad,
              u"、".join(bad) if bad else u"")
        review_payload(pkg, label)


# --------------------------------------------------------------------------- #
# 发布包内容审查（2026-09-25 加）
#
# 【为什么单独立一组】原来那几条只查**文件名**（有没有 applications.db 之类）。
# 但 2026-09-25 真出过一次"文件名全对、内容夹带"的事故：
# `services/profile.py` 里那份「默认个人画像」写的是**开发者本人的专业和目标城市**
# （材料工程 / 高分子材料与工程、广州/佛山/东莞/深圳/珠三角/广东），
# 跟着正式版发出去 —— 别人装上之后，程序"凭空"给他一份写着开发者方向的画像，
# 他的「对口判断」从第一天起就是按开发者的专业算的。
#
# **白名单只保证"不多带文件"，保证不了"带走的文件里没有别人的东西"。**
# 所以这一组查的是**内容**：
#   A. 默认画像必须是空白模板（结构性断言 —— 这条是主护栏）
#   B. 不许出现这台电脑绝对路径
#   C. 不许出现**真库里的**公司名（拿真库当探针，不是靠关键词猜）
#   D. 不许出现开发者姓名 + 他画像里的城市/专业词
#
# ⚠️ D 的探针**从 `个人画像.json` 现读**，不写死在脚本里 ——
#    写死的话画像一改探针就过期，检查会变成摆设（这个项目吃过文档漂移的亏）。
#
# ⚠️ **刻意不把「材料工程」「高分子」这类专业名本身当探针**：它们是常见学科名，
#    任何人的画像里都可能有，示例里出现一次并不指向谁。把它们当探针会让检查
#    "长期报红 → 所有人都学会忽略它"（`AGENTS.md` 里记过这个教训）。
#    真正指向个人的信号是 **A（默认值必须为空）+ 城市 + 姓名 + 真库公司名**，
#    这四样已经覆盖。默认画像里那几个字段一旦被填回具体值，A 立刻报红。
# --------------------------------------------------------------------------- #

# 允许出现的绝对路径（通用、与"谁的电脑"无关）
ALLOWED_PATH_HINTS = [u"C:\\Program Files", u"C:\\Program Files (x86)", u"C:\\Windows"]

# 离线依赖包：`app/wheels/` 里每个包至少要有这些文件（缺一个首次启动就装不上）。
#
# 【为什么单立一条】2026-09-25 之前，首次启动要**联网**装 pywebview；
# 实测在连不通的机器上会卡很久然后打印一堆红色 pip 报错（试用的人原话：
# "卡在第 1 步很久不下载"）。现在把依赖打进文件夹，这一步就离线了 ——
# 但**依赖包不能悄悄少**，所以要有检查盯着。
#
# 写法：包名 -> 该包必须出现的**文件名前缀**（不写死版本号，升级依赖时不用改这里）。
REQUIRED_WHEELS = {
    "pywebview": "pywebview-",
    "pythonnet": "pythonnet-",
    "clr_loader": "clr_loader-",
    "cffi": "cffi-",
    "pycparser": "pycparser-",
    "bottle": "bottle-",
    "proxy_tools": "proxy_tools-",
    "typing_extensions": "typing_extensions-",
    "openpyxl": "openpyxl-",
    "et_xmlfile": "et_xmlfile-",
}

# cffi 是**按 Python 版本分的二进制包**（py3-none-any 那种通用包不适用），
# 所以它必须同时提供 3.10 / 3.11 / 3.12 三份 —— 缺了对应版本，
# 那台机器就会**悄悄退回联网安装**（又回到卡住的老问题）。
REQUIRED_CFFI_TAGS = ("cp310", "cp311", "cp312")

# 城市探针要放过的"通用搭配"：这些串里出现的地名**不是**开发者画像里的城市。
# 目前只有学校名一种情况 —— 这个工具就是给这所学校的学生用的，
# 学校名出现在代码里是设计使然（同步接口、提示词都要写）。
ALLOWED_CITY_PHRASES = [u"广东工业大学", u"广工"]


def _read_any(path):
    """
    尽量把文件当文本读出来；二进制也**不跳过**。

    为什么要读二进制：这一组检查的教训是"文件名全对、内容夹带"。
    如果遇到 .whl / .xlsx 就直接跳过，那个盲区就又回来了。
    wheel 是 zip，用 utf-8 硬读会乱码，所以按 utf-8 → gbk → latin-1 依次兜底
    （latin-1 永远不会失败，能保证每个字节都被看到）。
    """
    raw = b""
    try:
        with io.open(path, "rb") as handle:
            raw = handle.read()
    except IOError:
        return u""
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return u""


def _text_files(pkg_dir):
    """列出包里的所有文件（含二进制 —— 见 `_read_any` 的说明）。"""
    out = []
    for dirpath, _dirs, files in os.walk(pkg_dir):
        for name in files:
            out.append(os.path.join(dirpath, name))
    return out


def review_payload(pkg_dir, label):
    """对一个已生成的发布包做内容审查（2026-09-25 加）。"""
    import json
    import re as _re

    texts = [(os.path.relpath(p, pkg_dir), _read_any(p)) for p in _text_files(pkg_dir)]
    by_rel = dict((rel.replace("/", "\\"), txt) for rel, txt in texts)

    # ---------- 0. 二进制文件里的"路径"不算 ----------
    # wheel / xlsx 是压缩包，随便一段字节都可能像 `X:\xxx`。对它们做"绝对路径"检查
    # 只会产生噪声（第一版就这么误报过：bottle 的 .whl 里报出 `J:\ZqjD:HSlkE...`）。
    # ⚠️ 注意：只**跳过路径检查**，不跳过"真库公司名/开发者姓名"那几项 ——
    #    那几项查的是有意义的字符串，二进制里也不该出现（保留了 `_read_any` 的兜底解码）。
    BINARY_EXT = (".whl", ".zip", ".xlsx", ".ico", ".png", ".jpg", ".db", ".gz")
    path_texts = [(rel, txt) for rel, txt in texts
                  if not rel.lower().endswith(BINARY_EXT)]

    # ---------- A. 默认画像必须是空白模板（结构性断言，主护栏） ----------
    # 为什么是"结构性"而不是"搜关键词"：搜关键词只能挡住**已知**的那几个词，
    # 换成另一个专业就漏了。这里直接读 `core/profile.py` 的 DEFAULT_PROFILE 常量，
    # 断言那四个"属于某个人"的字段**必须是空的**。
    core_prof = by_rel.get(u"core\\profile.py", u"")
    fields_ok, detail = False, u"找不到 core/profile.py"
    if core_prof:
        try:
            # 用 AST 从源码里抠字面量（不 import —— 包里的代码不该在本进程里跑）
            import ast
            tree = ast.parse(core_prof)
            default = None
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "DEFAULT_PROFILE"
                        for t in node.targets):
                    default = ast.literal_eval(node.value)
            if default is None:
                detail = u"没从 core/profile.py 抠到 DEFAULT_PROFILE"
            else:
                # 这四个字段一旦非空，就说明"某个人的画像"又被写进代码了
                personal = {
                    "major_primary": default.get("major_primary"),
                    "major_secondary": default.get("major_secondary"),
                    "city_keywords": default.get("city_keywords"),
                    "target_industries": default.get("target_industries"),
                    "major": default.get("major"),
                }
                dirty = [k for k, v in personal.items() if v]
                fields_ok = not dirty
                detail = (u"非空字段：%s" % u"、".join(dirty)) if dirty else u"四个字段都是空的"
        except Exception as exc:
            detail = u"解析失败：%s" % exc
    check(u"%s：默认画像的专业/城市/行业字段都是空的（不是某个人的画像）" % label,
          fields_ok, detail)

    # ---------- B. 这台电脑绝对路径 ----------
    # ⚠️ 匹配式必须**允许空格**：`C:\Program Files\Google\Chrome\...` 里带空格，
    #    第一版写成 `[^\s"']{4,}`，结果被空格截成 `C:\Program`，
    #    恰好躲过了下面那份白名单 —— 是检查自己的 bug（自测时抓到的）。
    path_hits = []
    for rel, txt in path_texts:
        for m in _re.findall(r"[A-Za-z]:\\[^\n\"'<>|*?]{4,}", txt):
            m = m.strip().rstrip(u"\\")
            if any(m.startswith(h) for h in ALLOWED_PATH_HINTS):
                continue
            path_hits.append(u"%s: %s" % (rel, m[:70]))
    check(u"%s：没有这台电脑绝对路径" % label, not path_hits,
          u"；".join(path_hits[:3]) if path_hits else u"")

    # ---------- C. 真库里的公司名（拿真库当探针） ----------
    # ⚠️ 两个坑（自测时都踩到）：
    #   ① **不要只取前 N 家**：第一版写了 `LIMIT 40`，结果「有研广东新材料」那家在
    #      第 40 家之后，它在 `core/talks.py` 注释里的痕迹就漏掉了。现在取**全部**。
    #   ② **探针要带「核心片段」**：真库里的名字多是全称
    #      （「某某科技股份有限公司」），而代码注释里往往只写简称（「某某科技」）。
    #      所以按常见后缀切出核心词一并当探针。
    #   ③ **最短 4 字**：切出来的短片段会撞通用词。实测「记忆」命中了
    #      「窗口位置/大小记忆」这种正常注释 —— 那是误报，会让检查失去价值。
    probes = []
    db = os.path.join(DATA, "applications.db")
    if os.path.isfile(db):
        try:
            import sqlite3
            conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
            names = []
            for table, column in (("applications", "company"), ("talks", "company")):
                try:
                    rows = conn.execute(
                        "SELECT DISTINCT %s FROM %s" % (column, table)).fetchall()
                except Exception:
                    rows = []
                for (name,) in rows:
                    name = (name or u"").strip()
                    if len(name) >= 4 and u"自检" not in name:
                        names.append(name)
            conn.close()
            probes = list(names)
            for name in names:
                for cut in (u"股份有限公司", u"有限责任公司", u"有限公司",
                            u"科技", u"集团", u"公司"):
                    core = name.split(cut)[0]
                    if cut in name and len(core) >= 4 and core not in probes:
                        probes.append(core)
        except Exception:
            probes = []
    db_hits = []
    for rel, txt in texts:
        for name in probes:
            if name and name in txt:
                db_hits.append(u"%s: %s" % (rel, name))
                break
    check(u"%s：没有真库里的公司名（探针 %d 个）" % (label, len(probes)),
          not db_hits, u"；".join(db_hits[:3]) if db_hits else u"")

    # ---------- D. 开发者姓名 + 他画像里的城市/行业词 ----------
    # 探针**从 个人画像.json 现读**，不写死 —— 写死的话画像一改探针就过期。
    who, personal_words = u"", []
    try:
        prof = json.loads(read(os.path.join(APP, u"个人画像.json"))) or {}
        who = str(prof.get("name") or u"").strip()
        # 城市是最指向个人的信号（专业名是常见学科名，城市名不是）
        personal_words = [str(c).strip() for c in (prof.get("city_keywords") or [])
                          if len(str(c).strip()) >= 2]
    except Exception:
        pass

    hits = []
    for rel, txt in texts:
        if who and who in txt:
            hits.append(u"%s: 姓名" % rel)
        for word in personal_words:
            if word not in txt:
                continue
            # ⚠️ 消掉**已知的通用用法**，否则检查会长期误报、最后被所有人忽略
            #    （AGENTS.md 里记过这个教训）。
            #    实测唯一需要放过的：`广东` 出现在学校名「广东工业大学」里 ——
            #    那是这个工具服务的学校，不是开发者画像里的城市。
            stripped = txt
            for ok_phrase in ALLOWED_CITY_PHRASES:
                stripped = stripped.replace(ok_phrase, u"")
            if word in stripped:
                hits.append(u"%s: %s" % (rel, word))
                break
    check(u"%s：没有开发者姓名/他画像里的城市（探针：%s）"
          % (label, u"、".join(([who] if who else []) + personal_words) or u"画像没填，跳过"),
          not hits, u"；".join(hits[:3]) if hits else u"")

    # ---------- E. 离线依赖包完整 ----------
    # 只对**发布包**查（个人版工作区里没有 wheels/ 是正常的：打包时才带上）。
    wheels_dir = os.path.join(pkg_dir, "wheels")
    if not os.path.isdir(wheels_dir):
        check(u"%s：带着离线依赖包 wheels/（首次启动不用联网）" % label, False,
              u"没有 wheels/ 目录 —— 首次启动会退回联网装 pywebview（连不通就会卡住）")
        return
    names = os.listdir(wheels_dir)
    missing = [pkg for pkg, prefix in REQUIRED_WHEELS.items()
               if not any(n.startswith(prefix) for n in names)]
    check(u"%s：离线依赖包齐全（%d 个包，共 %d 个文件）"
          % (label, len(REQUIRED_WHEELS), len(names)),
          not missing, u"缺：%s" % u"、".join(missing) if missing else u"")
    # cffi 要覆盖 3.10/3.11/3.12 三个 Python 版本
    cffi_tags = [t for t in REQUIRED_CFFI_TAGS
                 if any(n.startswith("cffi-") and t in n for n in names)]
    check(u"%s：cffi 覆盖 Python %s" % (label, u" / ".join(REQUIRED_CFFI_TAGS)),
          len(cffi_tags) == len(REQUIRED_CFFI_TAGS),
          u"只找到 %s" % u"、".join(cffi_tags) if len(cffi_tags) != len(REQUIRED_CFFI_TAGS) else u"")
    # 不许把源码包（.tar.gz）留在里面：装它要编译器，对方机器上多半没有
    sdists = [n for n in names if n.endswith((".tar.gz", ".zip"))]
    check(u"%s：离线依赖包里没有源码包（不需要编译器）" % label, not sdists,
          u"、".join(sdists) if sdists else u"")


def fingerprint():
    fp = {}
    for name in ("applications.db", "change_log.jsonl"):
        p = os.path.join(DATA, name)
        if os.path.isfile(p):
            h = hashlib.md5()
            with io.open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            fp[name] = {"md5": h.hexdigest(), "size": os.path.getsize(p)}
    backups = os.path.join(DATA, "backups")
    if os.path.isdir(backups):
        fp["_backup_dirs"] = sorted(os.listdir(backups))[-5:]
    return fp


def do_snapshot():
    fp = fingerprint()
    d = os.path.dirname(FINGER)
    if not os.path.isdir(d):
        os.makedirs(d)
    with io.open(FINGER, "w", encoding="utf-8") as f:
        f.write(json.dumps(fp, ensure_ascii=False, indent=1))
    print(u"已拍指纹：%s" % FINGER)
    for k, v in fp.items():
        if isinstance(v, dict):
            print(u"  %s  %s  %d 字节" % (k, v["md5"], v["size"]))
    return 0


def do_compare():
    try:
        before = json.loads(io.open(FINGER, encoding="utf-8").read())
    except (IOError, ValueError):
        print(u"没有指纹文件，先跑 --snapshot")
        return 1
    now = fingerprint()
    print(u"对比指纹：")
    changed = []
    for k in sorted(set(list(before.keys()) + list(now.keys()))):
        if k.startswith("_"):
            continue
        b, n = before.get(k), now.get(k)
        if b != n:
            changed.append(k)
            print(u"  [CHANGED] %s：%s -> %s" % (k, b, n))
        else:
            print(u"  [SAME]    %s  %s" % (k, (n or {}).get("md5", u"-")))
    if changed:
        print(u"结果：数据被改动了（%s）——违反红线" % u"、".join(changed))
        return 1
    print(u"结果：数据一个字节都没变")
    return 0


def main():
    if "--snapshot" in sys.argv[1:]:
        return do_snapshot()
    if "--compare" in sys.argv[1:]:
        return do_compare()
    static_checks()
    ok = all(results)
    print(u"结果：%s（共 %d 项）" % (u"全部通过" if ok else u"存在失败项", len(results)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
