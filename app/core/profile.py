# -*- coding: utf-8 -*-
"""
core/profile.py —— 个人画像的**纯规则**（2026-09-25 新增）

【为什么有这个文件】
原来「默认画像」是写死在 `services/profile.py` 里的一整份**具体画像**（某个人的专业、
目标城市、关键词表）。两个后果，都是真问题：

1. **夹带**：那份默认值被原样打进给别人的包里 —— 别人装上之后，
   程序"凭空"给他一份写着**开发者的专业和目标城市**的画像，他完全不知情。
2. **对别人功能是坏的**：他的「对口判断」从第一天起就是按开发者的方向算的。

根因是**代码和数据没分家**：`profile.py` 一个文件既提供 `load_profile()` 这个函数（代码），
又提供那份默认画像（**某一个人的数据**）。

现在拆成两半：
  - **本文件（`core/`）**：只放**纯规则** —— 默认值长什么样、怎么从「主/副专业」派生关键词、
    怎么判断画像填完了没有、怎么生成给人看的显示文本。给同样输入就给同样输出，能离线测。
  - **`services/profile.py`**：只负责读 / 写 / 合并那个 JSON 文件。

【规矩】core 不许碰文件、不许 import 上层（`tools/test_core.py` + `test_architecture.py` 用 AST 盯着）。
"""


# --------------------------------------------------------------------------- #
# 默认画像：**刻意是空的**（2026-09-25 改成空白模板）
#
# ⚠️ 这里**绝对不许**填入任何具体专业、城市、行业、岗位 —— 因为这份默认值会跟着
#    正式版发给别人。要放示例就放"中性且对谁都成立"的东西（例如 minus_keywords
#    里的「普工、两班倒」那种通用排除项）。
#    这条由 `tools/make_trial_package.py` 的发布包内容审查盯着（改回去就打包失败）。
# --------------------------------------------------------------------------- #

# 「跟专业无关、对谁都成立」的通用项 —— 这几项保留有实际价值：
#   plus_keywords  ：求职时普遍加分的东西（国企/双休/年假…）
#   minus_keywords ：普遍避开的（销售/客服/普工/倒班…）
# 而「专业 / 城市 / 行业 / 目标岗位」全部留空，等用户自己填。
DEFAULT_PROFILE = {
    "name": "",
    # 学历：默认硕士是安全的（本工具面向应届硕/本），用户可以改
    "degree": "硕士研究生",
    "graduate_year": "",

    # ---- 专业：主专业必填、副专业选填（2026-09-25 加）----
    # 为什么要分主/副：一个人可能是「材料工程」为主、「高分子材料与工程」为辅，
    # 两个都要参与对口判断，但主专业是必填的、决定性的。
    "major_primary": "",
    "major_secondary": "",

    # ---- 下面这些留空，由关键词派生或用户自己填 ----
    "target_positions": [],
    "major_keywords": [],
    "city_keywords": [],
    "target_industries": [],
    "position_keywords": [],

    # ---- 通用项（对谁都成立，保留默认值）----
    "plus_keywords": ["国有企业", "央企", "国企", "双休", "七险", "年假", "培养"],
    "minus_keywords": ["销售", "客服", "普工", "两班倒", "倒班", "车间", "管培生"],

    # 兼容字段（老版本的画像文件里可能只有这一个）：仅在 major_primary 为空时当作主专业读
    "major": "",
}


# 从「专业名」里切关键词时，剥掉这些**不承载专业信息**的后缀。
#
# ⚠️ 这份表是**故意短**的，而且只收"剥掉之后剩下的词仍然是一个有意义的专业词"的后缀。
#    反例（评测时真踩到）：`计算机科学与技术` 去掉后缀 `技术` 会剩下 `计算机科学与`
#    —— 那不是一个词，拿它去匹配招聘需求只会误判。所以 `技术` / `科学` 这类
#    **不收**：宁可少派生一个词，也不要派生出会骗人的词（用户 2026-09-25 的要求：
#    认不出来别硬猜 —— 同一个态度）。
#    实测：`材料工程` -> `材料`（对，招聘需求就写「材料类」「材料专业」）；
#          `计算机科学与技术` -> 只保留原样（对，岗位就写这个全名）。
_MAJOR_SUFFIXES = ("科学与工程", "与工程", "工程")
_MAJOR_SPLIT = ("/", "／", "、", ",", "，", "|", "；", ";")
_MAJOR_NOISE = ("专业", "方向", "类", "（", "）", "(", ")", " ")


def split_major(text):
    """
    把用户填的「主专业」或「副专业」拆成一个个专业名。

    容错：中英文斜杠、顿号、逗号、分号、竖线都当分隔符。
    例：`"材料工程 / 高分子材料与工程"` -> `["材料工程", "高分子材料与工程"]`
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    for sep in _MAJOR_SPLIT[1:]:
        raw = raw.replace(sep, _MAJOR_SPLIT[0])
    out = []
    for part in raw.split(_MAJOR_SPLIT[0]):
        part = part.strip()
        if part:
            out.append(part)
    return out


def derive_major_keywords(major_primary, major_secondary=""):
    """
    从「主专业 + 副专业」派生用于对口匹配的关键词表（**去重、保序**）。

    为什么不是简单地把专业名丢进去匹配：招聘需求里几乎不会写「材料工程」这四个字，
    它写的是「材料类」「高分子」「材料科学与工程」这类。所以关键词要**由专业名扩展**：
      ① 原样保留（"高分子材料与工程" —— 有些岗位确实原样写）
      ② 去掉类别后缀（"材料工程" -> "材料"）
      ③ 副专业整体也算一条

    派生的是**保守**的短词，宁可少不可多 —— 匹配词太宽会把不对口的判成对口
    （用户 2026-09-25 明确要求「认不出来别硬猜」，同一个态度）。

    给定同样输入永远同样输出（纯函数，可离线测）。
    """
    kws = []
    for name in split_major(major_primary) + split_major(major_secondary):
        if name and name not in kws:
            kws.append(name)
        # 去掉「工程」这类后缀，留下核心词：「材料工程」->「材料」。
        # 只剥 _MAJOR_SUFFIXES 里那几个 —— 理由见那份表上面的注释（剥错比不剥更糟）。
        for suffix in _MAJOR_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                core = _strip_noise(name[: -len(suffix)])
                # 剩下的必须**还是一个像样的词**（≥2 字）才算数
                if core and len(core) >= 2 and core not in kws:
                    kws.append(core)
                break
        # 「材料类」这种带噪声后缀的也留下核心
        core2 = _strip_noise(name)
        if core2 and core2 != name and len(core2) >= 2 and core2 not in kws:
            kws.append(core2)
    return kws


def _strip_noise(text):
    out = str(text or "")
    for noise in _MAJOR_NOISE:
        out = out.replace(noise, "")
    return out.strip()


def profile_needs_setup(profile):
    """
    这份画像是不是**还没填**（需要在界面上引导用户去填）。

    判定只看一个**必填项：主专业**。理由：其余字段（城市、行业、目标岗位）
    对「对口判断」的影响远小于专业，而且很多人一开始确实说不出具体城市 ——
    不该因为没填城市就把人拦在门外。

    兼容老文件：老版本只写了 `major` 字段的，也算填过了（不然老用户一升级就被弹窗）。
    """
    p = profile or {}
    if str(p.get("major_primary") or "").strip():
        return False
    if str(p.get("major") or "").strip():
        return False
    # 关键词表里有东西，也说明画像被人填过（老版本就是这么填的）
    if p.get("major_keywords"):
        return False
    return True


def normalize_profile(data, base=None):
    """
    把「文件里那份」和「默认值」合并成一份**字段齐全、类型正确**的画像。

    为什么需要它：用户手改 `个人画像.json` 时很可能只写几个键、或者把列表写成字符串。
    这里统一兜住，**绝不因为画像文件写得乱就让程序起不来**（这是原来的行为，保持）。

    合并规则（与老版本一致，只是多了主/副专业）：
      - 以 `base`（默认值）为底，文件里的键覆盖上去；
      - 列表型字段：字符串就按分隔符切、`None` 当空；
      - **关键词为空时按主/副专业自动派生**（用户不用自己抄一遍关键词）。
    """
    p = dict(base or DEFAULT_PROFILE)
    for key, value in (data or {}).items():
        p[key] = value

    # 类型兜底：列表型字段
    for key in ("target_positions", "major_keywords", "city_keywords",
                "target_industries", "position_keywords", "plus_keywords",
                "minus_keywords"):
        p[key] = _as_list(p.get(key))

    # 兼容老文件：只有 `major` 没有 `major_primary` 时，把它当主专业
    if not str(p.get("major_primary") or "").strip() and str(p.get("major") or "").strip():
        p["major_primary"] = str(p["major"]).strip()

    # 关键词派生：用户没自己写 major_keywords 时，从主/副专业推
    if not p["major_keywords"]:
        p["major_keywords"] = derive_major_keywords(
            p.get("major_primary"), p.get("major_secondary"))

    # `major` 保持和主专业同步（老代码 / 老文档还可能读它）
    p["major"] = str(p.get("major_primary") or "").strip()
    return p


def _as_list(value):
    """把各种可能写法统一成字符串列表（空值 -> []）。"""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v or "").strip()]
    text = str(value).strip()
    if not text:
        return []
    for sep in _MAJOR_SPLIT[1:]:
        text = text.replace(sep, _MAJOR_SPLIT[0])
    return [part.strip() for part in text.split(_MAJOR_SPLIT[0]) if part.strip()]


def profile_summary(profile):
    """
    给提示词/界面用的一句话画像描述（纯文本，同样的输入给同样的输出）。

    空画像时**明确说"未填写"**而不是硬套一个默认专业 —— 提示词里出现一个
    不是用户本人的专业，会把 AI 带到错的方向上去。
    """
    p = profile or {}
    degree = str(p.get("degree") or "").strip()
    primary = str(p.get("major_primary") or "").strip()
    secondary = str(p.get("major_secondary") or "").strip()
    majors = primary
    if secondary:
        majors = (primary + "（辅修 " + secondary + "）") if primary else secondary
    positions = "、".join(p.get("target_positions") or [])
    cities = "、".join(p.get("city_keywords") or [])
    return {
        "degree": degree or "（未填写）",
        "major": majors or "（未填写）",
        "major_keywords": "、".join(p.get("major_keywords") or []) or "（未填写）",
        "target_positions": positions or "（未填写）",
        "cities": cities or "（未填写）",
        "filled": not profile_needs_setup(p),
    }
