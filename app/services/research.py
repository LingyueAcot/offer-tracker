# -*- coding: utf-8 -*-
"""
services/research.py —— 调研：任务包组装 + 调研结果合并

2026-09-24 从 `server.py` 搬过来（S4）。搬的时候**只改了 import 的来源**，
业务逻辑一行没动。

【这一域在干什么】
1. `build_research_task()`：把库里的宣讲会按**公司**整理成一份给 AI 的调研任务包
   （含公司名、招聘需求原文、要问哪些字段的提示词）。同一家公司多场宣讲会只出现一次。
2. `apply_research(text)`：把 AI 回的结果贴回来，解析成结构化字段写进库。
   **核心字段 >= 3 个算「完整调研」，否则按字段合并**（保留原有内容不丢）。

【为什么搬】
`api/talks.py` 的 `/api/research_task` 与 `/api/import_research` 要用它们，
原来只能 `from server import ...`。

【规矩】
- 不 import `server`（靠 `store` / `core` / `services.audit` / `services.profile`）。
- **提示词文案（RESEARCH_PROMPT*）留在这里**：它是这一域的内容，不是通用常量。
  改文案时注意它和 `导入模板.xlsx` 里「给 AI 的提示词」那张表是同源的
  （见 server.py 顶部的说明）。
"""

import re
from datetime import datetime

import store as store_module
from core.research import (_fit_level_from_text, _match_talks,
                           parse_research_text)
from core.talks import evaluate_fit, recruit_digest
from core.values import as_int, clean, now_str

from services.audit import log_change
from services.profile import load_profile, profile_status, profile_summary


RESEARCH_SUMMARY_FIELDS = [
    "一句话总评",
    "对口判断", "对口岗位", "对口说明",
    "研发工艺岗薪资", "薪资与结构", "待遇与节奏",
    "加班与工时",
    "员工真实体验",
    "福利与补贴",
    "应届生转正", "应届生相关", "校招相关",
    "公司基本面",
    "其他岗位",
    "负面与媒体", "社交平台",
    "风险建议",
]

RESEARCH_PROMPT_LINES = [
    "请对下面每一家公司做一次校园招聘调研。",
    "**第一步先做「对口判断」**（我会在每家公司下面附上该校就业网抓到的招聘需求原文），",
    "**第二步以「研发岗 / 工艺岗的真实待遇与员工体验」为第一优先级**，然后严格按我给的格式输出。",
    "",
    "【调研对象画像（决定调研权重，不要偏离）】",
    "{{PROFILE}}",
    "",
    "【一、第一规则（最优先）：有时效性，优先采纳当年应届的信息】",
    "{{FIRST_RULE}}",
    "",
    "【二、调研重点与权重（按这个比例分配精力）】",
    "",
    "第一优先｜研发岗 / 工艺岗的**真实待遇与员工体验**（约 70% 精力，必须写实、写具体）",
    "注意：「待遇」不只是钱。下面几块都要写，尤其是**员工的真实感受**：",
    "- 【薪资】应届生（尤其硕士）在研发/工艺岗的具体数字与构成：基本工资 / 绩效 / 年终奖（几个月）/ 签字费 / 餐补住宿交通补贴，注明学历与城市",
    "- 【作息】日常几点上下班、大小周 / 单双休 / 是否倒班、加班费或调休怎么算、周末被消息打扰的频率",
    "- 【氛围与管理】团队氛围、领导风格（是否 PUA / 画饼 / 勾心斗角）、同事关系、mentor 是否真带教、能不能学到东西",
    "- 【稳定性】人员流动性 / 离职率、是否卡转正、有没有末位淘汰、应届生留得住吗",
    "- 【福利】五险一金与公积金比例、食宿条件（几人间 / 包不包吃）、班车、年假好不好请、体检",
    "- 【成长】应届生培养体系、轮岗机制、晋升通道、涨薪节奏",
    "",
    "第二优先｜公司稳定性（约 20% 精力）",
    "- 母公司 / 上市代码、该业务板块所处阶段、近年营收与利润、有无裁员欠薪劳动仲裁",
    "",
    "第三优先｜其他补充（约 10% 精力，简写即可，不要占篇幅）",
    "- 非研发岗位（销售 / 职能 / 生产）的大致情况、整体氛围、媒体报道",
    "- 与研发、工艺岗无关或信息很少的，一句「未找到」带过即可",
    "",
    "【三、怎么搜（重要：**评论区 / 跟帖往往比正文更真实**）】",
    "0. 先找帖子和评论区，再找官话：",
    "   - 牛客：面经帖 / offer 对比帖下面的**评论区**，常有「已入职半年来说说」「同 offer 的聊聊」；搜「公司名 2027届」「公司名 面经」「公司名 offer 选择」",
    "   - 知乎：高赞回答 + **该回答下面的评论**，尤其是匿名回答；搜「公司名 工作体验」「公司名 离职」「公司名 氛围」「公司名 加班」",
    "   - 看准网 / 职Q（智联）：员工点评**原文**，注意看岗位和在职时长；搜「公司名 员工评价」「公司名 工作体验」",
    "   - 脉脉：搜「公司名 吐槽」「公司名 离职」；公众号文章 / 小红书笔记下面的**留言**也要看",
    "   ⚠️ 判断原则：**正文常常是引流或客套，评论区往往才说真话**。",
    "   摘录时请标明来源类型，例如「评论区（现任研发）」「帖主（22届离职）」「点评原文（在职工艺 2 年）」。",
    "1. 薪资爆料：牛客搜「公司名 2027届 薪资」「公司名 offer」「公司名 应届生 薪资」",
    "2. 员工点评：看准网 / 职Q 搜「公司名 工资」；脉脉搜「公司名 吐槽」",
    "3. 社区长文：知乎、小红书搜「公司名 入职」「公司名 劝退」「公司名 避雷」",
    "4. 稳定性：搜「公司名 裁员」「公司名 欠薪」「公司名 劳动仲裁」「公司名 负面」",
    "5. ⚠️ 关于小红书：它是封闭的 App 生态，搜索接口不对外开放，搜索引擎基本抓不到内容。",
    "   能搜到就写；搜不到就写「小红书：未找到（内容为站内生态，搜索引擎抓不到）」，",
    "   不要在这件事上反复尝试，也不要编造小红书上的评价——**宁可写未找到**。",
    "   同理，贴吧、脉脉如果只能搜到零散内容，写明来源和年份即可。",
    "",
    "【四、对口判断（第一步就要做，而且必须做）】",
    "我在每家公司下面，附了**该校就业网详情页抓下来的招聘需求原文**，通常包含：",
    "- 单位性质 / 所属行业 / 人员规模",
    "- 职位列表：每个岗位的**岗位名称 ｜ 学历要求 ｜ 薪资 ｜ 工作城市 ｜ 专业要求**",
    "- 招聘简章里的关键段落（岗位需求、专业要求、培养与福利等）",
    "",
    "**请直接依据我给的这段原文判断对口，不要自己另搜岗位、不要做模糊筛选、不要跳过这一段。**",
    "判断标准（严格按这个来，不要凭感觉）：",
    "- ✅ 对口：职位列表里有 **2 个及以上** 岗位明确接受我的专业方向"
    "（专业要求的原文里出现这些词或它们的近义词：{{MAJOR_KEYWORDS}}），"
    "并且学历要求覆盖 {{DEGREE}}（写「{{DEGREE}}」「{{DEGREE}}研究生」或「本科及以上」都算）。",
    "- ⚠️ 部分对口：有相关岗位，但专业要求写得很宽（如只写「理工科」「专业不限」「相关专业」），"
    "或者明确对口的岗位只有 1 个。",
    "- ⚪ 不对口：职位列表里全是明确不相关的专业，"
    "或者只有销售、职能、生产操作类岗位。",
    "",
    "另外两个字段这样写：",
    "- `对口岗位`：从职位列表里**照抄岗位名称**，挑 2~4 个最值得我投的，按推荐顺序排列；"
    "每个岗位后面用括号补充「学历 ｜ 薪资 ｜ 城市」（招聘需求里没有的信息就省略）。",
    "- `对口说明`：一句话，**必须引用招聘需求里的原文片段**当依据。",
    "  例：「某某岗位的招聘需求里写明接受『{{MAJOR_KEYWORDS}}』，学历 {{DEGREE}}」。",
    "",
    "⚠️ 特殊情况：如果某家公司我**没有附招聘需求**（或附的内容里没有职位列表），"
    "就把 `对口判断` 写成「⚠️ 部分对口」，并在 `对口说明` 里写明「未抓到招聘需求，无法判断，需自行核对」——"
    "**不要猜，也不要直接判成不对口**。",
    "",
    "【五、怎么判断（很重要）】",
    "1. **时效优先**：同一条结论如果既有当年应届的信息、又有旧信息，**只采纳当年应届的**；",
    "   旧信息要么不写，要么必须标注「⚠️ 信息较旧（20XX 届 / 20XX 年），仅供参考」。",
    "2. 薪资、加班这两项，**至少 2 个独立来源互相印证**才算可信；只有一个来源必须注明「单一来源，未经交叉验证」。",
    "3. 每条尽量写清：平台 + **届数或年份** + 说话人身份（如「27届硕士研发」「在职工艺工程师」）+ 原话或具体数字。",
    "4. 区分「可核实事实 / 员工自述 / 未证实传言」；查不到写「未找到」，**严禁编造**，更不要把官网宣传当员工评价。",
    "5. 数字优先级：具体金额 > 区间 > 模糊形容（如「比较高」）。",
    "6. 如果研发/工艺岗和公司其他岗位待遇差别很大，**请分开写**，不要用平均数字糊过去。",
    "7. 氛围、管理、成长这类**主观感受**，一定要引**原话**并注明说话人身份与时间；",
    "   只有单一来源的要标注「单一来源，未经交叉验证」；多人提到同一问题（如都吐槽领导 PUA）要写成「多人提到」。",
    "8. 涉及具体的人（领导姓名、同事）请用「某主管」代替，不要写真实姓名。",
    "",
    "【六、输出格式（必须严格遵守）】",
    "1. 只输出文本块，不要 JSON、不要表格、不要代码块、不要任何前言、解释或总结。",
    "2. 每家公司一个块，块与块之间不要插其他文字。",
    "3. 每个块以 ===公司： 开头、以 ---结束--- 结尾，字段名称和顺序一个字都不能改。",
    "4. 字段名后面必须紧跟中文冒号「：」，内容可换行；从下一行起、以「- 」开头的行都算该字段的内容。",
    "5. 公司名称必须和清单完全一致，一个字都不要改。",
    "6. 口碑评级只能填：✅ 相对正面 / ⚠️ 需谨慎 / ❌ 风险较高",
    "7. 风险建议只能填：优先面 / 可面需追问 / 谨慎后置",
    "8. 对口判断只能填：✅ 对口 / ⚠️ 部分对口 / ⚪ 不对口（三个词一个字都不要改）",
    "9. 现场必问写 3~4 条，必须围绕研发/工艺岗的**待遇、氛围、加班与转正**，用「|」分隔；来源用「|」分隔，最多 6 条。",
    "",
    "【七、输出模板（照抄格式，内容按实际搜索结果填；不要保留「示例」二字）】",
    "===公司：公司名称===",
    "口碑评级：⚠️ 需谨慎",
    "一句话总评：30 字以内，重点体现研发/工艺岗的待遇",
    "行业类型：一句话说明该公司所属行业与业务",
    "对口判断：✅ 对口",
    "对口岗位：某某工程师（本科及以上 ｜ 8000-10000 ｜ 某市）；某某工程师（本科及以上 ｜ 8000-10000 ｜ 某市）",
    "对口说明：职位列表里「某某岗位」写明接受「{{MAJOR_KEYWORDS}}」，学历 {{DEGREE}}",
    "研发工艺岗薪资：",
    "- 牛客（27届硕士研发，2026-09）：“研发岗 18k×15，年包约 27 万，某市”",
    "- 看准 / 职Q（2026 年数据）：工艺工程师月薪区间 12-18k",
    "- ⚠️ 信息较旧（22届，2022 年）：当年硕士月薪约 12k，仅供参考",
    "- 薪资构成：基本 70% + 绩效 20% + 年终 2 个月；另有餐补 500/月、政府补贴 2.5 万/年×5 年",
    "- 交叉验证：2 个来源数字接近，可信度较高",
    "加班与工时：",
    "- 知乎（在职研发）：“日常 9:00 到 20:30，大小周，周末加班可调休”",
    "- 牛客（25届）：“工艺岗要跟线，偶尔需要倒班”",
    "员工真实体验：",
    "- 牛客评论区（现任研发，2026-09）：“氛围还行，组里人挺 nice，就是项目紧的时候连着加班”",
    "- 知乎（22届离职，匿名）：“领导比较 PUA，绩效打分主观，干得多不一定拿得多”",
    "- 看准网点评原文（在职工艺 2 年，2025）：“车间跟线比较累，办公室氛围还可以，食堂一般”",
    "- 交叉验证：3 条都提到加班强度大；2 条提到管理偏主观",
    "- ⚠️ 单一来源，未经交叉验证：有人说「卡转正」，仅 1 人提到",
    "福利与补贴：",
    "- 公积金 12%，提供住宿 / 房补 800，班车，年假 5 天起",
    "应届生转正：",
    "- 牛客（往届）：“转正率大概八成，考核看项目产出”（单一来源，未经交叉验证）",
    "公司基本面：",
    "- 母公司 / 上市代码、业务阶段、近年营收利润、有无裁员欠薪",
    "其他岗位：",
    "- 销售 / 职能岗的大致情况，一两句即可；无关可写「未找到」",
    "负面与媒体：",
    "- 未找到主流媒体报道；或写：2025-xx 某媒体报道了 xx 事件（附来源）",
    "社交平台：",
    "- 牛客：“…”；小红书：未找到；贴吧 / 脉脉：未找到",
    "风险建议：可面需追问",
    "现场必问：研发/工艺岗日常上下班时间、周末加班频率与调休规则？| 团队氛围与直属主管的管理风格是怎样的？| 硕士应届研发/工艺岗的薪资构成与调薪节奏？| 应届生转正考核标准与往年转正率？",
    "来源：https://xxx | https://yyy",
    "---结束---",
]

RESEARCH_PROMPT = "\n".join(RESEARCH_PROMPT_LINES)

RESEARCH_BATCH_SIZE = 8

SUMMARY_LABEL_RE = re.compile(r"^【([^】]{1,14})】", re.M)



def build_research_task(limit=None, start=None, end=None):
    """
    生成「调研任务包」文本：待调研公司清单 + 严格提示词。
    一次最多列 RESEARCH_BATCH_SIZE 家——一次给 AI 太多公司，质量会明显下降。
    导入这一批之后，再点一次会自动给出下一批。

    范围：**今天及以后、还没调研过的宣讲会**。
    —— 早先这里只扫「近三天」，结果今天 19 号、22 号的场次明明已经同步进列表了，
       却因为超出窗口而进不了任务包，只能干等两天。学校的**同步**窗口是近三天，
       但**调研**没必要卡这个限制：只要还没过期就该能提前调研。
    已取消 / 已过期的场次不列（反正也用不上了）。

    start / end 一般不用传（默认「今天起、不设上限」），留出参数是为了方便测试时
    指定别的日期区间。
    """
    if limit is None:
        limit = RESEARCH_BATCH_SIZE
    today = datetime.now().date()
    start = start or today.strftime("%Y-%m-%d")

    # 画像没填就没法生成任务包：提示词里的「对口判断」标准要按**我的专业**算，
    # 专业空着的话 AI 只能瞎猜，生成的结论全是废的。
    # 与其给一份会误导人的任务包，不如明确告诉用户先去填（2026-09-25 加）。
    status = profile_status()
    if status["needs_setup"]:
        return None, ("还没填个人画像 —— 请先在首页点「填写个人画像」，填好主专业再生成调研任务包，"
                      "否则 AI 无法判断哪些岗位对你对口。")

    conditions = [
        "source='gdut'",
        "talk_date >= ?",
        "status NOT IN ('已取消', '已过期')",
        "(rating = '' OR rating = '待补充')",
    ]
    params = [start]
    if end:
        conditions.append("talk_date <= ?")
        params.append(end)
    with store_module.db().locked_cursor() as cur:
        rows_all = [
            dict(r)
            for r in cur.execute(
                "SELECT * FROM talks WHERE %s ORDER BY talk_date ASC, id ASC"
                % " AND ".join(conditions),
                tuple(params),
            )
        ]
    # 同一家公司可能有多场宣讲会（例如某某科技有两场），调研是按「公司」做的，
    # 所以这里先按标准公司名合并成一家，避免同一家在任务包里出现两次、白占名额。
    groups, group_index = [], {}
    for row in rows_all:
        key = (row.get("company_std") or "").strip() or (row.get("company") or "").strip()
        if not key:
            continue
        if key not in group_index:
            group_index[key] = len(groups)
            groups.append({"key": key, "rows": []})
        groups[group_index[key]]["rows"].append(row)

    selected = groups[:limit]
    remaining = max(0, len(groups) - len(selected))
    # 把个人画像注入提示词，让调研聚焦到「研发/工艺岗的薪资待遇」
    # ⚠️ 2026-09-25：这段原来是**写死某个人的专业**的（材料/高分子），
    #    现在改成**从画像生成** —— 见 core/profile.py 的 profile_summary()。
    #    这样发给别人的正式版不会夹带开发者的专业，同学用的也是他自己的方向。
    profile = load_profile()
    summary = profile_summary(profile)
    profile_line = (
        "我是一名 %s（专业方向：%s），求职目标是 %s。意向城市：%s。"
        "请把调研精力优先放在这些岗位——尤其是**研发岗、工艺岗**——的**真实待遇与员工体验**上："
        "薪资、加班与作息、团队氛围与管理风格、福利、应届生培养与转正。"
        % (
            summary["degree"],
            summary["major"],
            summary["target_positions"],
            summary["cities"],
        )
    )
    # 第一规则：时效优先——优先采纳当年应届的信息
    now = datetime.now()
    grad_year = as_int(profile.get("graduate_year")) or (now.year + 1)
    season = "秋招" if now.month >= 7 else "春招"
    first_rule = (
        "现在是 %d 年%s，正在招的是 **%d 届**（也就是我的这一届）。"
        "只要搜到面向 %d 届、或 %d 年内发布的信息，**一律优先采纳**；"
        "如果只能找到更早的（例如 2023 届、2022 年发布），必须标注"
        "「⚠️ 信息较旧（20XX 届 / 20XX 年），仅供参考」；"
        "当年信息与旧信息冲突时，**以当年应届的信息为准**。"
        "每一条薪资、加班信息都要带上**届数或年份**。"
        % (now.year, season, grad_year, grad_year, now.year)
    )
    prompt = RESEARCH_PROMPT.replace("{{PROFILE}}", profile_line).replace(
        "{{FIRST_RULE}}", first_rule
    ).replace(
        # 对口判断那一段里的专业关键词和学历，也从画像生成（2026-09-25）
        "{{MAJOR_KEYWORDS}}", summary["major_keywords"]
    ).replace(
        "{{DEGREE}}", summary["degree"].replace("研究生", "") or "硕士"
    )
    lines = [
        "# 宣讲会企业调研任务包",
        "",
        "调研范围：%s"
        % (
            "%s ~ %s" % (start, end)
            if end
            else "%s 起（今天及以后、还没调研过的宣讲会）" % start
        ),
        "待调研公司：%d 家（本批 %d 家%s）" % (
            len(groups), len(selected),
            "，还有 %d 家放下一批" % remaining if remaining else "",
        ),
        "建议一次发给 AI 不超过 %d 家，质量更稳；导入本批后再点一次「📋 调研任务包」会自动给下一批。"
        % RESEARCH_BATCH_SIZE,
        "",
        "**第一规则（时效优先）**：优先采纳**当年应届**（%d 届 / %d 年）的信息；"
        "旧信息必须标注届数并注明「仅供参考」。" % (grad_year, now.year),
        "**对口判断**：每家公司下面都附了**该校就业网抓到的招聘需求原文**"
        "（岗位名称、学历、薪资、城市、专业要求）。AI 会据此直接给出「对口 / 部分对口 / 不对口」"
        "并推荐具体岗位——不是关键词模糊匹配，你看结论的时候可以对着原文复核。",
        "**调研权重**：研发/工艺岗**真实待遇与员工体验**（薪资/加班/氛围/福利/转正）约 70%"
        " · 公司稳定性 约 20% · 其他补充 约 10%",
        "**重点看跟帖与评论区**：正文常常是客套，评论区往往才说真话。",
        "（以上都已写进提示词，公司清单也会带上你的岗位画像）",
        "",
        "## 一、公司清单（连同下面的提示词一起发给 DeepSeek，公司名不要改）",
        "",
    ]
    for index, group in enumerate(selected, start=1):
        rows = group["rows"]
        # 展示名取最短的那个：一般最干净（「某某科技股份有限公司」而不是「…宣讲会」）
        # ⚠️ 这里原来举的是**真公司名**——这份文件会进正式版，已改成虚构示例（2026-09-25）。
        names = [(row.get("company") or "").strip() for row in rows]
        names = [n for n in names if n]
        display = min(names, key=len) if names else group["key"]
        when = "；".join(
            " ".join(
                part for part in (row.get("talk_date") or "", row.get("time_slot") or "",
                                  row.get("location") or "") if part
            )
            for row in rows
        )
        lines.append("%d. %s（%s）" % (index, display, when or "时间地点待补充"))
        # 多场宣讲会的话，把各场抓到的招聘需求合并去重（通常内容一样）
        digest, seen = [], set()
        for row in rows:
            for line in recruit_digest(row):
                if line not in seen:
                    seen.add(line)
                    digest.append(line)
        if digest:
            lines.append("")
            lines.append("   招聘需求（从广东工业大学就业网详情页抓下来的原文，**请只依据这段判断对口**）：")
            lines += ["   " + line for line in digest]
        else:
            lines.append("")
            lines.append("   招聘需求：未抓到（详情页没有岗位信息，对口判断请按提示词里的「特殊情况」处理）")
    if not selected:
        lines.append("（暂无待调研公司，说明近三天的公司都已经调研过了）")
    elif remaining:
        lines += ["", "> 本批之后还剩 %d 家：导入本批结果后，再点一次「📋 调研任务包」即可。" % remaining]
    lines += ["", "## 二、提示词", "", prompt, ""]
    lines += ["## 三、把 DeepSeek 的输出整段复制回来，粘贴到「宣讲会 → 📥 导入调研结果」即可", ""]
    return "\n".join(lines), None



def apply_research(text):
    """把粘贴回来的调研结果写进对应的宣讲会记录（支持完整调研与局部补充）。

    ⚠️ 入口先把 `text` 收敛成字符串（2026-09-27 修）：这个值是 HTTP 请求里的
    `payload.get("text")`，直接喂给 `parse_research_text()` 时，只要它不是字符串
    （字典 / 数字 / 列表），正则的 `finditer` 就会抛 `TypeError`，一路冒到
    `_run_chain` 变成 500「后台出错」。这里按项目惯例用 `clean()` 收敛，
    非字符串会变成空串 -> 走下面那条「没有解析到公司块」的 400，而不是 500。
    """
    entries = parse_research_text(clean(text))
    if not entries:
        return {
            "ok": False,
            "error": "没有解析到公司块。请确认每段都以「===公司：公司名===」开头、以「---结束---」结尾",
        }
    profile = load_profile()
    updated, unmatched = [], []
    updated_records = 0
    stamp = now_str()
    with store_module.db().locked_cursor() as cur:
        talks = [dict(r) for r in cur.execute("SELECT * FROM talks ORDER BY id DESC")]
        for entry in entries:
            targets = _match_talks(entry, talks)
            if not targets:
                unmatched.append(entry.get("company") or "")
                continue
            # 区分「完整调研」和「只补充了某几项」：
            #   核心字段 ≥3 个 -> 完整调研，摘要整体替换
            #   否则（例如只粘贴了「社交平台」一条）-> 按字段合并，保留原有内容不丢
            core_labels = ["一句话总评", "研发工艺岗薪资", "薪资与结构", "待遇与节奏",
                           "加班与工时", "福利与补贴", "应届生转正", "应届生相关"]
            core_hits = sum(
                1 for label in core_labels if (entry.get(label) or "").strip()
            )
            updates = {}
            for label in RESEARCH_SUMMARY_FIELDS:
                value = (entry.get(label) or "").strip()
                if value:
                    updates[label] = value
            question_text = "\n".join(
                piece.strip()
                for piece in re.split(r"[|｜\n]+", entry.get("现场必问") or "")
                if piece.strip()
            )
            # 同一家公司可能有多场宣讲会 -> 每一场都写上这份调研结果
            for target in targets:
                before = dict(target)
                if core_hits >= 3:
                    merged_pairs = [
                        (label, updates[label])
                        for label in RESEARCH_SUMMARY_FIELDS
                        if label in updates
                    ]
                else:
                    merged_pairs = []
                    seen = set()
                    for label, content in _split_summary(before.get("summary") or ""):
                        if label in updates:
                            merged_pairs.append((label, updates[label]))
                            seen.add(label)
                        else:
                            merged_pairs.append((label, content))
                    for label in RESEARCH_SUMMARY_FIELDS:
                        if label in updates and label not in seen:
                            merged_pairs.append((label, updates[label]))
                summary_text = "\n".join(
                    "【%s】%s" % (label, content)
                    for label, content in merged_pairs
                    if content
                )
                new_rating = (entry.get("口碑评级") or "").strip()
                # 调研里的「对口判断」取代关键词初判：
                # 关键词匹配是「文本里有没有出现专业词」，AI 是「照着招聘需求读一遍」，
                # 后者明显更准，所以调研结果一进来就写进 fit_level / fit_reason。
                # 只有用户自己手动点过标签的（fit_by='manual'）才不覆盖。
                manual_locked = (before.get("fit_by") or "") == "manual"
                fit_value = (entry.get("对口判断") or "").strip()
                fit_level = "" if manual_locked else _fit_level_from_text(fit_value)
                fit_reason = ""
                if not manual_locked:
                    reason_bits = []
                    note = (entry.get("对口说明") or "").strip()
                    jobs = "；".join(
                        piece.strip()
                        for piece in re.split(r"[；\n]+", entry.get("对口岗位") or "")
                        if piece.strip()
                    )
                    if note:
                        reason_bits.append(note)
                    if jobs:
                        reason_bits.append("推荐岗位：" + jobs)
                    fit_reason = "  ".join(reason_bits)
                # 局部补充时不覆盖已有内容，只补齐新给的字段
                cur.execute(
                    """
                    UPDATE talks SET rating=?, summary=?, questions=?,
                        research_industry=?, research_sources=?, research_at=?, updated_at=?,
                        fit_level=COALESCE(NULLIF(?, ''), fit_level),
                        fit_reason=COALESCE(NULLIF(?, ''), fit_reason),
                        fit_manual=CASE WHEN ? <> '' THEN 1 ELSE fit_manual END,
                        fit_by=CASE WHEN ? <> '' THEN 'research' ELSE fit_by END
                    WHERE id=?
                    """,
                    (
                        new_rating or (before.get("rating") or ""),
                        summary_text,
                        question_text or (before.get("questions") or ""),
                        (entry.get("行业类型") or "").strip() or (before.get("research_industry") or ""),
                        (entry.get("来源") or "").strip() or (before.get("research_sources") or ""),
                        stamp, stamp,
                        fit_level, fit_reason, fit_level, fit_level,
                        target["id"],
                    ),
                )
                updated_records += 1
                if (target.get("company") or "") not in updated:
                    updated.append(target.get("company") or "")
                log_change(
                    "research", "talk", target["id"],
                    "导入企业调研：%s（%s%s）" % (
                        target.get("company"),
                        entry.get("口碑评级") or "",
                        "，对口判断 " + fit_value if fit_value and not manual_locked else "",
                    ),
                    before=before,
                    after=dict(
                        target,
                        rating=entry.get("口碑评级") or "",
                        fit_level=fit_level or (before.get("fit_level") or ""),
                    ),
                )
                talks = [
                    dict(
                        t,
                        rating=entry.get("口碑评级") or "",
                        fit_level=fit_level or (t.get("fit_level") or ""),
                        fit_reason=fit_reason or (t.get("fit_reason") or ""),
                        fit_by=("research" if fit_level else (t.get("fit_by") or "")),
                    )
                    if t["id"] == target["id"] else t
                    for t in talks
                ]
        # 调研可能带来新的行业信息 -> 重新做对口初判（手动改过的跳过）
        cur.execute("SELECT * FROM talks WHERE fit_manual=0")
        for row in [dict(r) for r in cur.fetchall()]:
            level, reason = evaluate_fit(row, profile)
            cur.execute(
                "UPDATE talks SET fit_level=?, fit_reason=? WHERE id=?",
                (level, reason, row["id"]),
            )
    return {
        "ok": True,
        "updated": updated,          # 命中并更新的公司名（去重）
        "records": updated_records,  # 实际更新的宣讲会场次数量
        "unmatched": unmatched,
    }



def _split_summary(text):
    """
    把已有摘要按【字段】拆成 [(字段, 内容), ...]。
    局部补充（例如只粘贴小红书那一条）时用它做字段级合并，避免把已调研的内容冲掉。
    """
    pairs = []
    if not text:
        return pairs
    matches = list(SUMMARY_LABEL_RE.finditer(text))
    for index, match in enumerate(matches):
        label = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pairs.append((label, text[start:end].strip("\n ")))
    return pairs

