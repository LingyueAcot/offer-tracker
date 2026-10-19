# -*- coding: utf-8 -*-
"""
core/ 纯函数的回归测试 —— 不用开程序、不碰真实数据。

用法：
    python tools\test_core.py

【为什么有它】
2026-09-22 把 server.py 里的纯函数（阶段推导 / 统计 / 宣讲会解析 / 调研解析）逐字搬进了
app/core/。搬完得有东西证明「行为没变」，以后改这些规则也得有东西兜着——这就是这份测试。
它只 import core/*，不起服务、不读 data/、不碰网络。

【一条约定】
core/ 里只许放**纯函数**：不 import server、不 import sqlite3 / urllib / subprocess /
socket / threading，也不许 open() 文件。下面 TestCoreBoundaries 用 AST 读源码盯着这条，
不是靠自觉。
"""
from __future__ import print_function

import ast
import os
import sys
import unittest
from datetime import date, datetime

sys.dont_write_bytecode = True          # 别在 app\ 里留 __pycache__
sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
CORE_DIR = os.path.join(APP_DIR, "core")
sys.path.insert(0, APP_DIR)

from core import excel as excel_rules  # noqa: E402
from core import packaging, research, stages, stats, talk_sync, talks, values  # noqa: E402
from core.research import RESEARCH_FIELDS  # noqa: E402
from core.talks import (  # noqa: E402
    NOT_A_COMPANY_CATEGORY, brief_from_detail, category_from_unit_nature, company_key,
    evaluate_fit, format_job_list, html_to_lines, normalize_company, parse_talk_detail,
    parse_talk_list, parse_talk_time, recruit_digest, strip_noise_tail, talk_values,
)


class TestValues(unittest.TestCase):
    def test_clean_去空去零字节并截断(self):
        """clean()：None → 空串、去掉 \x00、两边去空白、超长截断"""
        self.assertEqual(values.clean(None), "")
        self.assertEqual(values.clean("  a\x00b  "), "ab")
        self.assertEqual(values.clean("abcdef", 3), "abc")
        self.assertEqual(values.clean(123), "123")

    def test_as_int_只认整数否则None(self):
        """as_int()：能转就转，不能转给 None（不抛异常）"""
        self.assertEqual(values.as_int("12"), 12)
        self.assertEqual(values.as_int(7), 7)
        self.assertIsNone(values.as_int("abc"))
        self.assertIsNone(values.as_int(None))

    def test_now_and_today_格式一致(self):
        """now_str() / today_str()：格式固定，而且 today 就是 now 的前 10 位"""
        now = values.now_str()
        today = values.today_str()
        self.assertRegex(now, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertRegex(today, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(today, now[:10])

    def test_parse_excel_date_能认的几种写法(self):
        """_parse_excel_date()：Excel 里日期的几种实际写法都要认"""
        self.assertEqual(values._parse_excel_date(datetime(2026, 9, 16, 13, 0)), "2026-09-16")
        self.assertEqual(values._parse_excel_date(date(2026, 9, 6)), "2026-09-06")
        self.assertEqual(values._parse_excel_date("2026-09-16"), "2026-09-16")
        self.assertEqual(values._parse_excel_date("2026/9/6"), "2026-09-06")
        self.assertEqual(values._parse_excel_date("2026.9.6"), "2026-09-06")
        self.assertEqual(values._parse_excel_date("2026年9月16日"), "2026-09-16")
        # 没有年份的按今年算
        self.assertEqual(values._parse_excel_date("9月16日"), "%04d-09-16" % datetime.now().year)
        # 认不出来就原样返回（宁可留着让用户看见，也不要悄悄丢掉）
        self.assertEqual(values._parse_excel_date("待定"), "待定")
        self.assertEqual(values._parse_excel_date(""), "")
        self.assertEqual(values._parse_excel_date(None), "")


class TestStages(unittest.TestCase):
    def test_derive_status_只看最后一条进展(self):
        """derive_status()：状态由最后一条进展决定，历史不影响"""
        self.assertEqual(stages.derive_status([]), "未登记")
        self.assertEqual(stages.derive_status([{"stage": "投递", "result": "已完成"}]), "进行中")
        self.assertEqual(
            stages.derive_status([{"stage": "面试", "result": "进行中"}, {"stage": "Offer", "result": ""}]),
            "Offer")
        # 前面被拒过、后来又面试了，应该还是「进行中」
        self.assertEqual(
            stages.derive_status([{"stage": "简历初筛", "result": "未通过"}, {"stage": "面试", "result": "进行中"}]),
            "进行中")

    def test_derive_status_四种结束状态(self):
        """derive_status()：已拒绝 / 已放弃 / Offer / 进行中 各走各的分支"""
        self.assertEqual(stages.derive_status([{"stage": "简历初筛", "result": "未通过"}]), "已拒绝")
        self.assertEqual(stages.derive_status([{"stage": "已拒绝", "result": ""}]), "已拒绝")
        self.assertEqual(stages.derive_status([{"stage": "二面", "result": "已放弃"}]), "已放弃")
        self.assertEqual(stages.derive_status([{"stage": "已放弃", "result": ""}]), "已放弃")
        # 字段缺失也不能炸
        self.assertEqual(stages.derive_status([{}]), "进行中")

    # ---- attach_derived_fields：2026-09-24 从 server.py 的 build_state() 搬来的 ----
    # 搬之前这段计算夹在「开连接读四张表」和「拼响应字典」中间，
    # 只能开着程序手工试；现在能离线喂固定数据验。

    def test_派生字段_没有进展的投递(self):
        """没有进展：三个字段给空串、event_count=0、状态是「未登记」"""
        apps = [{"id": 1, "company": "甲"}]
        stages.attach_derived_fields(apps, [])
        self.assertEqual(apps[0]["events"], [])
        self.assertEqual(apps[0]["event_count"], 0)
        self.assertEqual(apps[0]["current_stage"], "")
        self.assertEqual(apps[0]["current_result"], "")
        self.assertEqual(apps[0]["last_event_at"], "")
        self.assertEqual(apps[0]["derived_status"], "未登记")

    def test_派生字段_取最后一条进展(self):
        """有进展：current_* 取**最后一条**（列表顺序），last_event_at 取它的 created_at"""
        apps = [{"id": 1}, {"id": 2}]
        events = [
            {"application_id": 1, "stage": "投递", "result": "已完成", "created_at": "2026-09-01 09:00:00"},
            {"application_id": 2, "stage": "笔试", "result": "进行中", "created_at": "2026-09-02 09:00:00"},
            {"application_id": 1, "stage": "一面", "result": "进行中", "created_at": "2026-09-03 09:00:00"},
        ]
        stages.attach_derived_fields(apps, events)
        self.assertEqual(apps[0]["event_count"], 2)
        self.assertEqual(apps[0]["current_stage"], "一面")
        self.assertEqual(apps[0]["last_event_at"], "2026-09-03 09:00:00")
        self.assertEqual(apps[1]["event_count"], 1)
        self.assertEqual(apps[1]["current_stage"], "笔试")
        # 每条投递只拿到自己的进展（不能串）
        self.assertEqual([e["application_id"] for e in apps[0]["events"]], [1, 1])
        self.assertEqual([e["application_id"] for e in apps[1]["events"]], [2])

    def test_派生字段_事件里的空值补成空串(self):
        """进展里 stage/result/created_at 是 None 或缺字段 -> 补成空串，不是 None"""
        apps = [{"id": 1}]
        stages.attach_derived_fields(apps, [{"application_id": 1, "stage": None, "result": None}])
        self.assertEqual(apps[0]["current_stage"], "")
        self.assertEqual(apps[0]["current_result"], "")
        self.assertEqual(apps[0]["last_event_at"], "")

    def test_派生字段_状态由最后一条决定(self):
        """derived_status 跟着最后一条走，历史不影响"""
        apps = [{"id": 1}]
        stages.attach_derived_fields(apps, [
            {"application_id": 1, "stage": "简历初筛", "result": "未通过", "created_at": "t1"},
            {"application_id": 1, "stage": "Offer", "result": "", "created_at": "t2"},
        ])
        self.assertEqual(apps[0]["derived_status"], "Offer")

    def test_派生字段_没有进展的投递不会拿到别人的进展(self):
        """一条有进展、一条没有：没有的那条不能被串上别人的 events"""
        apps = [{"id": 1}, {"id": 2}]
        stages.attach_derived_fields(apps, [
            {"application_id": 1, "stage": "投递", "result": "已完成", "created_at": "t"},
        ])
        self.assertEqual(apps[1]["events"], [])
        self.assertEqual(apps[1]["derived_status"], "未登记")

    def test_派生字段_就地返回同一个列表(self):
        """返回的就是传进去那个列表（build_state() 依赖这个行为接着用）"""
        apps = [{"id": 1}]
        out = stages.attach_derived_fields(apps, [])
        self.assertIs(out, apps)


class TestStats(unittest.TestCase):
    def test_compute_stats_口径(self):
        """compute_stats()：总数 / 进行中 / Offer / 未登记 / 分类 / 今日与本周登记数"""
        today = values.today_str()
        apps = [
            {"derived_status": "进行中", "category": "私企",
             "events": [{"created_at": today + " 10:00:00"}, {"created_at": "2000-01-01 10:00:00"}]},
            {"derived_status": "Offer", "category": "央国企", "events": []},
            {"category": "未分类", "events": []},          # 没有 derived_status = 未登记
        ]
        result = stats.compute_stats(apps)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["active"], 1)
        self.assertEqual(result["offer"], 1)
        self.assertEqual(result["untouched"], 1)
        self.assertEqual(result["rejected"], 0)
        self.assertEqual(result["today_events"], 1)
        self.assertEqual(result["week_events"], 1)          # 2000 年那条不算本周
        self.assertEqual(result["by_category"], {"私企": 1, "央国企": 1, "未分类": 1})


class TestTalks(unittest.TestCase):
    def test_html_to_lines_去掉脚本样式和标签(self):
        """html_to_lines()：script/style 丢掉、标签换成换行、实体解码"""
        out = html_to_lines("<p>甲公司</p><script>var x=1;</script><style>a{}</style>b&nbsp;c")
        self.assertEqual(out, ["甲公司", "b c"])

    def test_parse_talk_time_带不带时间段都行(self):
        """parse_talk_time()：拆出 (日期, 时间段)，认不出就给两个空串"""
        self.assertEqual(parse_talk_time("2026-09-16 14:30-16:30（周三）"), ("2026-09-16", "14:30-16:30"))
        self.assertEqual(parse_talk_time("2026-09-16"), ("2026-09-16", ""))
        self.assertEqual(parse_talk_time("待定"), ("", ""))

    def test_normalize_company_清洗出标准公司名(self):
        """normalize_company()：去掉届数/秋招/宣讲会等噪声，圆点取最后一段，前缀标签去掉"""
        self.assertEqual(normalize_company("示例集团·示例科技2027届秋招宣讲会"), "示例科技")
        self.assertEqual(normalize_company("【央国企】示例科技有限公司"), "示例科技有限公司")
        self.assertEqual(normalize_company("腾讯科技2027届秋季校园招聘宣讲会"), "腾讯科技")
        self.assertEqual(normalize_company(""), "")
        self.assertEqual(normalize_company(None), "")

    def test_company_key_归一键去掉后缀与括号(self):
        """company_key()：匹配用的归一键——去公司后缀、去括号标点"""
        self.assertEqual(company_key("广东美的智享有限公司"), "广东美的智享")
        self.assertEqual(company_key("有研（广东）新材料有限公司"), "有研广东新材料")
        # 退化输入（真实数据里不会出现）：这里只是把现状钉住，不是主张这么干——
        # 等长时 有限公司 那一条被 len(text) > len(suffix) 挡住，接着命中「公司」再剥一次
        self.assertEqual(company_key("有限公司"), "有限")
        # 只有一个字的名字不会被剥空
        self.assertEqual(company_key("公司"), "公司")

    def test_parse_talk_list_解析列表页(self):
        """parse_talk_list()：列表页一条记录 → gdut_id / 公司 / 地点 / 日期时间段 / 线上线下"""
        html = (
            '<ul class="infoList teachinList">'
            '<li class="odd"><a href="https://career.gdut.edu.cn/teachin/id/12345" '
            'title="腾讯科技2027届秋季校园招聘宣讲会">腾讯科技2027届秋季校园招聘宣讲会</a></li>'
            '<li>大学城校区教1-301</li>'
            '<li>2026-09-16 14:30-16:30（周三）</li>'
            '<li><span class="status-text">线下</span></li>'
            '</ul>'
        )
        items = parse_talk_list(html)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["gdut_id"], "12345")
        self.assertEqual(item["company"], "腾讯科技2027届秋季校园招聘宣讲会")
        self.assertEqual(item["company_std"], "腾讯科技")
        self.assertEqual(item["location"], "大学城校区教1-301")
        self.assertEqual(item["talk_date"], "2026-09-16")
        self.assertEqual(item["time_slot"], "14:30-16:30")
        self.assertEqual(item["talk_type"], "线下")
        self.assertIn("/id/12345", item["source_url"])

    def test_parse_talk_detail_拆分简章与职位列表(self):
        """parse_talk_detail()：单位性质等按「标签行 + 值行」取，详情与职位列表按标题切"""
        html = (
            "<div>单位性质</div><div>民营企业</div>"
            "<div>单位行业</div><div>计算机、通信</div>"
            "<div>单位规模</div><div>1000人以上</div>"
            "<div>宣讲类别</div><div>线下宣讲</div>"
            "<div>宣讲会详情</div><div>招聘岗位：工艺工程师，要求硕士学历</div>"
            "<div>职位列表</div><div>01</div><div>工艺工程师</div>"
        )
        info = parse_talk_detail(html)
        self.assertEqual(info["unit_nature"], "民营企业")
        self.assertEqual(info["unit_industry"], "计算机、通信")
        self.assertEqual(info["unit_scale"], "1000人以上")
        self.assertEqual(info["talk_type"], "线下")
        self.assertIn("工艺工程师", info["detail_text"])
        self.assertNotIn("职位列表", info["detail_text"])   # 职位列表不算简章正文
        self.assertIn("01", info["positions"])

    def test_strip_noise_tail_截掉学校页脚(self):
        """strip_noise_tail()：黏在专业要求后面的学校联系方式要截掉"""
        raw = "高分子材料与工程、大学城校区：020-39325251（用人单位校园宣讲）"
        self.assertEqual(strip_noise_tail(raw), "高分子材料与工程")
        self.assertEqual(strip_noise_tail(""), "")
        self.assertEqual(strip_noise_tail("不分专业"), "不分专业")

    def test_format_job_list_整理成一行一个岗位(self):
        """format_job_list()：平铺单元格 → 「序号 ｜ 岗位 ｜ 学历 ｜ 薪资 ｜ 城市 ｜ 专业」"""
        positions = "\n".join([
            "序号", "职位信息", "需求专业", "操作",
            "01", "现场工艺工程师", "8000-10000", "广东省中山市", "全职",
            "本科及以上", "【材料类】高分子材料与工程、材料成型及控制工程", "投递简历",
            "02", "销售工程师", "面议", "广州市", "不限专业",
        ])
        rows = format_job_list(positions)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows[0],
            "01 ｜ 现场工艺工程师 ｜ 本科及以上 ｜ 8000-10000 ｜ 广东省中山市"
            " ｜ 专业：【材料类】高分子材料与工程、材料成型及控制工程",
        )
        # 「不限专业」是关键信息，不能被当噪音丢掉
        self.assertIn("不限专业", rows[1])
        self.assertIn("面议", rows[1])
        # 空输入 / 页面结构变了：返回空列表，调用方会退回原始文本
        self.assertEqual(format_job_list(""), [])
        self.assertEqual(format_job_list(None), [])

    def test_format_job_list_遵守上限(self):
        """format_job_list()：岗位数按 limit 截断（提示词不能被撑爆）"""
        positions = "\n".join(
            line for i in range(6)
            for line in ["%02d" % (i + 1), "岗位%d" % (i + 1), "本科", "10000-20000", "深圳市"]
        )
        self.assertEqual(len(format_job_list(positions, limit=3)), 3)

    def test_brief_from_detail_挑关键句并滤掉页脚(self):
        """brief_from_detail()：命中岗位/学历/薪资的句子优先，页脚噪音直接丢"""
        detail = "\n".join([
            "单位简介：一家做材料的公司",
            "邮箱：hr@example.com",
            "招聘岗位：工艺工程师，要求硕士学历，薪资 10000-15000",
        ])
        brief = brief_from_detail(detail)
        self.assertIn("招聘岗位", brief)
        self.assertNotIn("邮箱", brief)
        self.assertNotIn("单位简介", brief)      # 有命中的句子时就不退回全文

    def test_brief_from_detail_一句都没命中时退回全文(self):
        """brief_from_detail()：一句关键词都没命中时，退回「去掉页脚的全部内容」"""
        brief = brief_from_detail("公司介绍第一段\n公司介绍第二段")
        self.assertIn("公司介绍第一段", brief)
        self.assertIn("公司介绍第二段", brief)
        self.assertEqual(brief_from_detail(""), "")

    def test_recruit_digest_拼成给AI看的几行(self):
        """recruit_digest()：单位性质 / 职位列表 / 简章关键段落 三段"""
        talk = {
            "unit_nature": "民营企业", "unit_industry": "制造业", "unit_scale": "1000人以上",
            "positions": "\n".join(["01", "工艺工程师", "本科", "9000-12000", "中山市"]),
            "detail_text": "招聘岗位：工艺工程师，要求硕士",
        }
        out = "\n".join(recruit_digest(talk))
        # 这一行用的是「  ｜ 」（两个空格）——是原样搬过来的格式，故意钉住不让它悄悄变
        self.assertIn("单位性质：民营企业  ｜ 行业：制造业  ｜ 规模：1000人以上", out)
        self.assertIn("职位列表（岗位 ｜ 学历 ｜ 薪资 ｜ 城市 ｜ 专业要求）：", out)
        self.assertIn("工艺工程师", out)
        self.assertIn("招聘简章关键段落：", out)

    def test_recruit_digest_整理不出来时退回原始文本(self):
        """recruit_digest()：页面结构变了、职位列表整理不出来时，退回原始文本并标明"""
        talk = {"positions": "这一行不是标准职位列表", "detail_text": ""}
        out = "\n".join(recruit_digest(talk))
        self.assertIn("职位列表（原始文本，未整理）：", out)

    def test_evaluate_fit_四个分支(self):
        """evaluate_fit()：专业+硕士=强；只命中专业/只命中行业=部分；都没命中=不对口"""
        # 注意：「新材料」里含「材料」两个字，如果专业关键词写「材料」，这条用例会走成专业命中。
        # 所以这里把专业/行业词分开写，才能真的测到「只命中行业」那一支。
        profile = {"major_keywords": ["高分子"], "target_industries": ["新能源"],
                   "minus_keywords": ["倒班"]}
        level, reason = evaluate_fit({"detail_text": "招高分子专业，要求硕士"}, profile)
        self.assertEqual(level, "strong")
        self.assertIn("高分子", reason)

        level, _ = evaluate_fit({"detail_text": "招高分子专业，本科即可"}, profile)
        self.assertEqual(level, "mid")

        level, reason = evaluate_fit({"unit_industry": "新能源行业"}, profile)
        self.assertEqual(level, "mid")
        self.assertIn("新能源", reason)
        self.assertIn("行业相关", reason)

        level, reason = evaluate_fit({"detail_text": "招销售，倒班"}, profile)
        self.assertEqual(level, "low")
        self.assertIn("注意：倒班", reason)

        # 画像缺字段也不能炸
        self.assertEqual(evaluate_fit({}, {})[0], "low")

    def test_category_from_unit_nature_六种输入(self):
        """
        category_from_unit_nature()：把就业网的「单位性质」原文翻成投递类别。

        为什么单独立一条：宣讲会标「已投递」时会自动同步出一条投递记录，
        它的 category 原来被**写死成「宣讲会」**，于是「数据总览 · 投递类别」里
        混进一个跟「私企/央国企/外企」不是一类的东西（用户 2026-09-25 报的问题）。
        现在按单位性质归类 —— 归错会污染统计，所以边界必须逐个测。
        """
        # ① 央国企
        self.assertEqual(category_from_unit_nature("国有企业"), "央国企")
        self.assertEqual(category_from_unit_nature("中央企业"), "央国企")
        self.assertEqual(category_from_unit_nature("事业单位"), "央国企")
        # ② 外企
        self.assertEqual(category_from_unit_nature("外商投资企业"), "外企")
        self.assertEqual(category_from_unit_nature("中外合资"), "外企")
        # ③ 私企（就业网里最常见的写法）
        self.assertEqual(category_from_unit_nature("其他企业（含民营企业等）"), "私企")
        self.assertEqual(category_from_unit_nature("民营企业"), "私企")
        # ④ 两头都沾的：按更具体的「国企」算（规则顺序有意义，先判国企再判民营）
        self.assertEqual(category_from_unit_nature("国有控股民营企业"), "央国企")
        # ⑤ 认不出来 -> 空字符串，**绝不硬猜**（界面显示「未分类」）
        self.assertEqual(category_from_unit_nature("其他"), "")
        self.assertEqual(category_from_unit_nature("未知"), "")
        # ⑥ 空值不能炸
        self.assertEqual(category_from_unit_nature(""), "")
        self.assertEqual(category_from_unit_nature(None), "")

    def test_category_from_unit_nature_真实数据全覆盖(self):
        """
        拿**库里实际出现过的 5 种原文**逐条过一遍（2026-09-25 从 130 场宣讲会统计出来的）。

        这条是防回归的关键：以后谁改了关键词表，只要把这 5 种里的任意一种改坏，这里就红。
        实测分布：其他企业（含民营企业等）99 · 国有企业 15 · 外商投资企业 9 · 其他 5 · 未知 2
        """
        cases = {
            "其他企业（含民营企业等）": "私企",
            "国有企业": "央国企",
            "外商投资企业": "外企",
            "其他": "",
            "未知": "",
        }
        for raw, want in cases.items():
            self.assertEqual(category_from_unit_nature(raw), want,
                             "「%s」应该归成 %r" % (raw, want))

    def test_不是企业性质的类别名单(self):
        """NOT_A_COMPANY_CATEGORY：图表要排除的类别；它必须和页面里的同名常量一致。"""
        self.assertIn("宣讲会", NOT_A_COMPANY_CATEGORY)
        # 页面里也有一份（index.html 的 NOT_A_COMPANY_CATEGORY）——两处不能跑偏。
        # 真正的"两边一致"由 tools/verify_desktop.js 的静态检查盯着（那需要开程序）。
        # 这里至少保证 core 这份本身是有内容的、不是空壳。
        self.assertTrue(len(NOT_A_COMPANY_CATEGORY) >= 1)

    def test_talk_values_默认值与截断(self):
        """talk_values()：前端没传的字段给默认值，状态默认「待参加」"""
        row = talk_values({})
        self.assertEqual(row[0], "")                       # talk_date
        self.assertEqual(row[5], "待参加")                  # status 有默认
        row = talk_values({"company": " 甲公司 ", "status": "", "on_site": "有笔面试"})
        self.assertEqual(row[1], "甲公司")
        self.assertEqual(row[5], "待参加")
        self.assertEqual(row[4], "有笔面试")


class TestResearch(unittest.TestCase):
    SAMPLE = "\n".join([
        "===公司：腾讯科技===",
        "口碑评级：⚠️ 一般",
        "加班与工时：加班较多",
        "- 来源：牛客网评论区",
        "---结束---",
        "===公司：华为技术===",
        "一句话总评：节奏快",
    ])

    def test_parse_research_text_多行续行与多家公司(self):
        """parse_research_text()：续行要接上、结束标记之后不再读、多家公司都认"""
        entries = research.parse_research_text(self.SAMPLE)
        self.assertEqual(len(entries), 2)
        first = entries[0]
        self.assertEqual(first["company"], "腾讯科技")
        self.assertEqual(first["口碑评级"], "⚠️ 一般")
        self.assertEqual(first["加班与工时"], "加班较多\n- 来源：牛客网评论区")
        self.assertEqual(entries[1]["company"], "华为技术")
        self.assertEqual(entries[1]["一句话总评"], "节奏快")

    def test_parse_research_text_不认识的字段算续行(self):
        """parse_research_text()：只有白名单字段名才算新字段，其余行并到上一字段"""
        entries = research.parse_research_text("===公司：甲公司===\n一句话总评：还行\n补充说明：这行不是白名单字段")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["一句话总评"], "还行\n补充说明：这行不是白名单字段")
        self.assertEqual(research.parse_research_text(""), [])
        self.assertIn("一句话总评", RESEARCH_FIELDS)

    def test_match_talks_同一家公司有多场时全部返回(self):
        """_match_talks()：同一家公司两场宣讲会要都返回（只更第一场会「导入后不显示」）"""
        talks_list = [
            {"id": 1, "company": "腾讯科技", "company_std": "腾讯科技"},
            {"id": 2, "company": "腾讯科技", "company_std": "腾讯科技"},
            {"id": 3, "company": "华为技术有限公司", "company_std": "华为技术有限公司"},
        ]
        hit = research._match_talks({"company": "腾讯科技"}, talks_list)
        self.assertEqual([t["id"] for t in hit], [1, 2])
        # 公司名只写到一半：用「互相包含」兜住
        hit = research._match_talks({"company": "华为技术"}, talks_list)
        self.assertEqual([t["id"] for t in hit], [3])
        self.assertEqual(research._match_talks({"company": ""}, talks_list), [])

    def test_fit_level_from_text_先判不对口(self):
        """_fit_level_from_text()：先判「不对口」——否则「部分对口」会被误判成强对口"""
        self.assertEqual(research._fit_level_from_text("✅ 对口"), "strong")
        self.assertEqual(research._fit_level_from_text("⚠️ 部分对口"), "mid")
        self.assertEqual(research._fit_level_from_text("⚪ 不对口"), "low")
        self.assertEqual(research._fit_level_from_text("无法判断"), "")
        self.assertEqual(research._fit_level_from_text(""), "")
        self.assertEqual(research._fit_level_from_text(None), "")

    def test_split_position_note_与_merge_notes(self):
        """_split_position_note() 拆『——注：』；_merge_notes() 用「；」拼非空备注"""
        self.assertEqual(research._split_position_note("工艺工程师——注：需要倒班"),
                         ("工艺工程师", "需要倒班"))
        self.assertEqual(research._split_position_note("普通岗位"), ("普通岗位", ""))
        self.assertEqual(research._merge_notes("甲", "", None, " 乙 "), "甲；乙")
        self.assertEqual(research._merge_notes(), "")


class TestTalkSync(unittest.TestCase):
    """宣讲会同步的**判断**（core/talk_sync.py）—— 这块以前夹在 SQL 里，一条测试都没有"""

    def _row(self, **kw):
        row = {"id": 1, "source": "gdut", "status": "待参加",
               "gdut_id": "1", "talk_date": "2026-09-16"}
        row.update(kw)
        return row

    def test_merge_key_优先gdut_id再退公司加日期(self):
        """merge_key()：有 gdut_id 用它；没有就用「公司 + 日期」"""
        self.assertEqual(
            talk_sync.merge_key({"gdut_id": "12345", "company": "甲", "talk_date": "2026-09-16"}),
            "12345")
        self.assertEqual(
            talk_sync.merge_key({"gdut_id": "", "company": "甲", "talk_date": "2026-09-16"}),
            "甲2026-09-16")
        self.assertEqual(talk_sync.merge_key({}), "")

    def test_dedupe_items_分页重复只留第一条(self):
        """dedupe_items()：同一场在分页里出现多次只留第一条，顺序不变"""
        items = [
            {"gdut_id": "1", "company": "甲", "talk_date": "2026-09-16"},
            {"gdut_id": "2", "company": "乙", "talk_date": "2026-09-16"},
            {"gdut_id": "1", "company": "甲", "talk_date": "2026-09-16", "location": "第二页又出现"},
        ]
        out = talk_sync.dedupe_items(items)
        self.assertEqual([it["gdut_id"] for it in out], ["1", "2"])
        self.assertNotIn("location", out[0])          # 留下的是第一条，不是后来那条

    def test_dedupe_items_没有id时按公司加日期判重(self):
        """dedupe_items()：没有 gdut_id 时按「公司+日期」——同公司不同日、同日不同公司都不算重复"""
        items = [
            {"gdut_id": "", "company": "甲", "talk_date": "2026-09-16"},
            {"gdut_id": "", "company": "甲", "talk_date": "2026-09-17"},
            {"gdut_id": "", "company": "乙", "talk_date": "2026-09-16"},
            {"gdut_id": "", "company": "甲", "talk_date": "2026-09-16"},
        ]
        self.assertEqual(len(talk_sync.dedupe_items(items)), 3)

    def test_match_loose_先同名再子串(self):
        """match_loose()：先按归一化公司名完全相同找，找不到才退一步按子串匹配"""
        loose = [{"id": 1, "company": "广东美的智享有限公司"},
                 {"id": 2, "company": "某某科技（广州）有限公司"}]
        same = talk_sync.match_loose(loose, {"company": "广东美的智享有限公司", "company_std": ""})
        self.assertEqual(same["id"], 1)
        # 「美的智享」是「广东美的智享」的子串 —— 演示会那种截短的公司名也能配上
        near = talk_sync.match_loose(loose, {"company": "美的智享有限公司", "company_std": ""})
        self.assertEqual(near["id"], 1)

    def test_match_loose_短名字不许乱配(self):
        """match_loose()：走子串那一步要求**候选那边**归一键长度 >= 3，免得「华为」这种短名字乱配"""
        self.assertIsNone(talk_sync.match_loose([{"id": 1, "company": "华为"}],
                                                {"company": "华为技术有限公司", "company_std": ""}))
        # 归一键为空（公司名全是噪声词）时直接放弃，不要乱配
        self.assertIsNone(talk_sync.match_loose([{"id": 1, "company": "甲公司"}],
                                                {"company": "宣讲会", "company_std": ""}))

    def test_match_loose_不改入参而且不会配两次(self):
        """match_loose()：不改 candidates；调用方命中后自己 remove，第二条就不会并到同一条上"""
        loose = [{"id": 1, "company": "甲公司"}]
        first = talk_sync.match_loose(loose, {"company": "甲公司", "company_std": ""})
        self.assertEqual(len(loose), 1)               # 没被偷偷删掉
        loose.remove(first)
        self.assertIsNone(talk_sync.match_loose(loose, {"company": "甲公司", "company_std": ""}))

    def test_items_needing_detail_抓过的不再抓(self):
        """items_needing_detail()：库里已经有 detail_text 的跳过，其余（含没有记录的）都要抓"""
        items = [{"gdut_id": "1"}, {"gdut_id": "2"}, {"gdut_id": "3"}]
        existing = {"1": {"detail_text": "已经抓过"}, "2": {"detail_text": ""}}
        need = talk_sync.items_needing_detail(items, existing)
        self.assertEqual([it["gdut_id"] for it in need], ["2", "3"])

    def test_plan_cleanup_页面上撤了的标已取消(self):
        """plan_cleanup()：窗口内、以前同步来的、这次没抓到的 -> 已取消（学校页面上撤了）"""
        rows = [self._row(id=7, gdut_id="7", talk_date="2026-09-17")]
        self.assertEqual(talk_sync.plan_cleanup(rows, ["1", "2"], "2026-09-16", "2026-09-18"),
                         [(7, "已取消")])

    def test_plan_cleanup_日期过了还挂着待参加的标已过期(self):
        """plan_cleanup()：日期已经过去、状态还是「待参加」的 -> 已过期（自动收进底部折叠区）"""
        rows = [self._row(id=8, gdut_id="8", talk_date="2026-09-01")]
        self.assertEqual(talk_sync.plan_cleanup(rows, [], "2026-09-16", "2026-09-18"),
                         [(8, "已过期")])

    def test_plan_cleanup_只动自己同步来的和还挂着待参加的(self):
        """plan_cleanup()：手填的（source 不是 gdut）、你改过状态的（已参加/已投递…）一律不碰"""
        rows = [
            self._row(id=1, source="", talk_date="2026-09-01"),        # 你手填的
            self._row(id=2, status="已参加", talk_date="2026-09-01"),  # 你已经标过已参加
            self._row(id=3, status="已投递", gdut_id="9"),             # 已经进投递流程
            self._row(id=4, talk_date=""),                             # 日期都没填
            self._row(id=5, gdut_id="5", talk_date="2026-09-20"),      # 窗口之后，且这次抓到了
        ]
        self.assertEqual(talk_sync.plan_cleanup(rows, ["5"], "2026-09-16", "2026-09-18"), [])

    def test_plan_cleanup_没解析出id时不做消失判定(self):
        """plan_cleanup()：imported_ids 为空 -> 只做「日期已过」，绝不把整页标成已取消"""
        rows = [self._row(id=1, gdut_id="1", talk_date="2026-09-17"),   # 窗口内但没抓到
                self._row(id=2, gdut_id="2", talk_date="2026-09-01")]
        self.assertEqual(talk_sync.plan_cleanup(rows, [], "2026-09-16", "2026-09-18"),
                         [(2, "已过期")])

    def test_plan_cleanup_同一个id不会既取消又过期(self):
        """plan_cleanup()：两条规则不重叠，同一个 id 最多返回一次（否则统计会翻倍）"""
        rows = [self._row(id=1, gdut_id="1", talk_date="2026-09-01"),
                self._row(id=2, gdut_id="2", talk_date="2026-09-17")]
        out = talk_sync.plan_cleanup(rows, ["9"], "2026-09-16", "2026-09-18")
        ids = [talk_id for talk_id, _ in out]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(out), [(1, "已过期"), (2, "已取消")])


class TestExcelRules(unittest.TestCase):
    """「导入模板.xlsx」的解析规则（core/excel.py）—— 以前这一块的自检数是 0"""

    def test_挑工作表_两边都去空格(self):
        """pick_sheet_name()：名字两边有空格也能认出来；找不到返回 None（调用方自己兜底）"""
        self.assertEqual(excel_rules.pick_sheet_name([" 投递记录 ", "宣讲会"], "投递记录"), " 投递记录 ")
        self.assertIsNone(excel_rules.pick_sheet_name(["Sheet1"], "投递记录"))

    def test_表头与单元格取值(self):
        """header_of() / cell_text()：数字、None、越界都不能炸"""
        self.assertEqual(excel_rules.header_of([" 公司 ", None, 2026]), ["公司", "", "2026"])
        row = ["甲公司", None, 123]
        self.assertEqual(excel_rules.cell_text(row, 0), "甲公司")
        self.assertEqual(excel_rules.cell_text(row, 1), "")
        self.assertEqual(excel_rules.cell_text(row, 2), "123")
        self.assertEqual(excel_rules.cell_text(row, 9), "")      # 越界
        self.assertEqual(excel_rules.cell_text(row, None), "")   # 这一列没有

    def test_认列名别名(self):
        """application_columns()：用户改过表头（企业/职位/时间…）也要认；缺「公司」时给 None"""
        cols = excel_rules.application_columns(
            excel_rules.header_of(["企业", "类型", "职位", "时间", "渠道", "链接", "备注",
                                   "阶段", "结果", "进度"]))
        self.assertEqual(cols["company"], 0)
        self.assertEqual(cols["category"], 1)
        self.assertEqual(cols["position"], 2)
        self.assertEqual(cols["date"], 3)
        self.assertEqual(cols["detail"], 9)
        missing = excel_rules.application_columns(excel_rules.header_of(["岗位", "备注"]))
        self.assertIsNone(missing["company"])

    def test_宣讲会表的列名(self):
        """talk_columns()：「宣讲会日期」这种别名也要认"""
        cols = excel_rules.talk_columns(
            excel_rules.header_of(["宣讲会日期", "企业", "时间", "地点", "现场", "状态",
                                   "口碑", "摘要", "必问", "备注"]))
        self.assertEqual((cols["date"], cols["company"], cols["status"], cols["rating"]),
                         (0, 1, 5, 6))

    def test_示例行要跳过(self):
        """is_sample()：空公司名、以「示例」开头的整行跳过；正常公司不跳"""
        self.assertTrue(excel_rules.is_sample(""))
        self.assertTrue(excel_rules.is_sample("示例：某某科技有限公司"))
        self.assertFalse(excel_rules.is_sample("潮州三环"))

    def test_进展明细的拆法(self):
        """parse_detail_events()：分号或换行分隔，竖线分「阶段|结果|日期」；缺结果算进行中、缺日期用投递日期"""
        got = excel_rules.parse_detail_events("投递|已完成|2026-06-13；AI测评|进行中", "2026-06-01")
        self.assertEqual(got, [("投递", "已完成", "2026-06-13"), ("AI测评", "进行中", "2026-06-01")])
        # 换行 / 全角竖线 / 只有阶段名，三种都要认
        self.assertEqual(excel_rules.parse_detail_events("一面\n二面｜通过｜2026-07-01", "2026-06-01"),
                         [("一面", "进行中", "2026-06-01"), ("二面", "通过", "2026-07-01")])
        # 空阶段的那条直接丢掉；整列空就没有事件
        self.assertEqual(excel_rules.parse_detail_events("|已完成；体检", "2026-06-01"),
                         [("体检", "进行中", "2026-06-01")])
        self.assertEqual(excel_rules.parse_detail_events("", "2026-06-01"), [])
        self.assertEqual(excel_rules.parse_detail_events("  ；  ", "2026-06-01"), [])

    def test_没有明细时用当前阶段当前结果(self):
        """row_events()：没填「进展明细」就退回「当前阶段/当前结果」两列"""
        self.assertEqual(excel_rules.row_events("", "", "", "2026-06-01"),
                         [("投递", "已完成", "2026-06-01")])
        self.assertEqual(excel_rules.row_events("", "AI测评", "", "2026-06-01"),
                         [("AI测评", "进行中", "2026-06-01")])
        self.assertEqual(excel_rules.row_events("", "一面", "通过", "2026-06-01"),
                         [("一面", "通过", "2026-06-01")])
        # 有明细时明细说了算，阶段列被忽略
        self.assertEqual(excel_rules.row_events("投递|已完成", "一面", "通过", "2026-06-01"),
                         [("投递", "已完成", "2026-06-01")])


class TestPackaging(unittest.TestCase):
    """标准化数据包（core/packaging.py）—— 导入默认是「覆盖」，规则必须钉死"""

    def test_数据包形状检查(self):
        """validate_package()：不是字典 / 缺 applications / 不是数组都要报错；resumes 可选但要数组"""
        self.assertEqual(packaging.validate_package([]), "数据格式不正确")
        self.assertEqual(packaging.validate_package({}), "缺少 applications 字段")
        self.assertEqual(packaging.validate_package({"applications": "x"}), "applications 必须是数组")
        self.assertIsNone(packaging.validate_package({"applications": []}))
        # 简历（2026-10-10 加）：老包没有这个键也必须能导；有的话形状要对
        self.assertIsNone(packaging.validate_package({"applications": [], "resumes": []}))
        self.assertEqual(packaging.validate_package({"applications": [], "resumes": "x"}),
                         "resumes 必须是数组")

    def test_导入时逐条规整(self):
        """package_entries()：没公司名的跳过、阶段缺省「投递」、结果缺省「进行中」、时间戳兜底"""
        payload = {"applications": [
            {"company": " 甲公司 ", "events": [{"stage": "", "result": ""}, {"stage": "一面"}]},
            {"company": "   "},                      # 没公司名 → 整条跳过
            "不是字典",                               # 脏数据 → 跳过
            {"company": "乙公司"},                    # 一条进展都没有 → 空数组
        ]}
        got = packaging.package_entries(payload, "2026-09-22 10:00:00")
        self.assertEqual([a["company"] for a in got], ["甲公司", "乙公司"])
        self.assertEqual(got[0]["created_at"], "2026-09-22 10:00:00")
        self.assertEqual(got[0]["events"][0]["stage"], "投递")
        self.assertEqual(got[0]["events"][0]["result"], "进行中")
        self.assertEqual(got[0]["events"][1]["stage"], "一面")
        self.assertEqual(got[1]["events"], [])

    def test_导入阶段表(self):
        """package_stages()：字典形式和字符串形式都认，排序缺省 500，空名字丢掉"""
        got = packaging.package_stages({"stages": [
            {"name": "AI测评", "sort_order": 300, "is_preset": True},
            "笔试", "", None, {"name": ""},
        ]})
        self.assertEqual(got, [
            {"name": "AI测评", "sort_order": 300, "is_preset": 1},
            {"name": "笔试", "sort_order": 500, "is_preset": 0},
        ])

    def test_导入宣讲会(self):
        """package_talks()：没公司名跳过、状态缺省「待参加」、时间戳兜底"""
        got = packaging.package_talks({"talks": [
            {"company": "甲公司", "talk_date": "2026-09-16"},
            {"company": ""},
        ]}, "2026-09-22 10:00:00")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["status"], "待参加")
        self.assertEqual(got[0]["created_at"], "2026-09-22 10:00:00")
        self.assertEqual(got[0]["talk_date"], "2026-09-16")

    def test_导入简历(self):
        """package_resumes()：正文/排版走上同一套归一化；畸形内容退回默认结构，不中断导入"""
        got = packaging.package_resumes({"resumes": [
            {"title": " 我的简历 ", "is_default": True,
             "content": {"basics": {"name": "张三"}, "education": [{"school": "广工"}]},
             "layout": {"font_scale": 9}},
            {"title": "", "content": "不是对象"},
            "不是字典",
        ]}, "2026-10-10 10:00:00")
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["title"], "我的简历")
        self.assertEqual(got[0]["is_default"], 1)
        self.assertEqual(got[0]["content"]["basics"]["name"], "张三")
        self.assertEqual(got[0]["content"]["education"][0]["id"], "edu_1")   # 归一化补上稳定 id
        self.assertEqual(got[0]["layout"]["font_scale"], 1.4)                # 越界钳到上限
        self.assertEqual(got[0]["created_at"], "2026-10-10 10:00:00")
        self.assertEqual(got[1]["title"], "未命名简历")
        self.assertEqual(got[1]["content"], packaging.default_content())      # 畸形 → 默认结构

    def test_导出数据包的字段(self):
        """export_package()：schema / 版本号 / 时间戳 / 四块数据都在（漏了以后就恢复不回来）"""
        state = {"applications": [{"id": 1}], "stages": [{"name": "投递"}], "talks": [{"id": 2}]}
        got = packaging.export_package(state, 2, "1.1.8", "2026-09-22 10:00:00",
                                       resumes=[{"title": "我的简历"}])
        self.assertEqual(sorted(got.keys()),
                         ["app_version", "applications", "exported_at", "resumes",
                          "schema", "schema_version", "stages", "talks"])
        self.assertEqual(got["schema"], "resume-tracker")
        self.assertEqual(got["applications"], state["applications"])
        self.assertEqual(got["talks"], state["talks"])
        self.assertEqual(got["resumes"], [{"title": "我的简历"}])
        # 老调用点（只传四个参数）不许崩：简历位置给空数组
        self.assertEqual(packaging.export_package(state, 2, "1.1.8", "t")["resumes"], [])

    def test_CSV导出的行(self):
        """csv_rows()：列数和表头一致，「完整进展」把时间线压成「阶段(结果) @ 时间 备注」"""
        state = {"applications": [{
            "company": "甲公司", "category": "私企", "position": "工艺", "applied_date": "2026-06-13",
            "channel": "网申", "current_stage": "AI测评", "current_result": "进行中",
            "derived_status": "进行中", "event_count": 2, "last_event_at": "2026-09-01 10:00:00",
            "notes": "备注", "link": "https://example.com",
            "events": [
                {"stage": "投递", "result": "已完成", "created_at": "2026-06-13 09:00:00", "note": ""},
                {"stage": "AI测评", "result": "进行中", "created_at": "2026-09-01 10:00:00", "note": "限时"},
            ],
        }]}
        rows = packaging.csv_rows(state)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), len(packaging.csv_header()))
        self.assertEqual(rows[0][0], "甲公司")
        self.assertEqual(rows[0][-2],
                         "投递(已完成) @ 2026-06-13 09:00:00 | AI测评(进行中) @ 2026-09-01 10:00:00 限时")
        self.assertEqual(packaging.csv_rows({"applications": []}), [])

    def test_数据模型说明(self):
        """schema_description()：三个存储路径、五个实体（2026-10-10 加 resume）、接口清单都在"""
        got = packaging.schema_description(2, "1.1.8", {"dir": "/a", "database": "/b", "change_log": "/c"})
        self.assertEqual(got["storage"], {"dir": "/a", "database": "/b", "change_log": "/c"})
        self.assertEqual(sorted(got["entities"].keys()),
                         ["application", "event", "resume", "stage", "talk"])
        self.assertIn("POST /api/import", got["endpoints"])
        # 简历的接口必须出现在说明里（加了接口不同步这个文件，前端/用户就查不到）
        self.assertIn("PUT /api/resumes/<id>", got["endpoints"])
        self.assertIn("rev", got["entities"]["resume"])
        self.assertEqual(got["schema_version"], 2)


class TestCoreBoundaries(unittest.TestCase):
    # ⚠️ 2026-09-24 补网眼：原来这个名单少了 os / io / json / pathlib / ssl / tkinter ——
    # 而这几个**全都能拿来读写文件**，等于规矩写着「不许碰文件」、检查却留着后门。
    # 今天 core/ 一个都没用（已经验证过），补上是为了以后别从这里溜进去。
    # 更全面的分层检查在 tools/test_architecture.py（那个脚本管 core/ api/ store 三层）。
    FORBIDDEN_IMPORTS = {"server", "backup", "sqlite3", "urllib", "subprocess", "socket",
                         "threading", "webbrowser", "http", "shutil", "tempfile", "requests",
                         "os", "io", "json", "pathlib", "ssl", "tkinter"}

    def _core_sources(self):
        for name in sorted(os.listdir(CORE_DIR)):
            if name.endswith(".py"):
                path = os.path.join(CORE_DIR, name)
                with open(path, encoding="utf-8") as handle:
                    yield name, handle.read()

    def test_core_不依赖外部与副作用(self):
        """core/ 只许放纯函数：不许 import server/sqlite3/urllib/...，也不许 open() 文件"""
        for name, src in self._core_sources():
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        self.assertNotIn(root, self.FORBIDDEN_IMPORTS,
                                         "%s 里 import 了 %s" % (name, alias.name))
                elif isinstance(node, ast.ImportFrom):
                    root = (node.module or "").split(".")[0]
                    self.assertNotIn(root, self.FORBIDDEN_IMPORTS,
                                     "%s 里 from %s import ..." % (name, node.module))
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotEqual(node.func.id, "open",
                                        "%s 里有 open()：core 不该碰文件" % name)

    def test_server_用的就是_core_而不是自己留了一份(self):
        """server.py 里的名字必须就是 core 里那一个（防止又抄一份回去，两份慢慢跑偏）"""
        import server  # noqa: E402  （import server 不会启动服务，main() 在 __main__ 里）
        pairs = [
            (server.derive_status, stages.derive_status),
            (server.attach_derived_fields, stages.attach_derived_fields),
            (server.compute_stats, stats.compute_stats),
            (server.parse_talk_list, talks.parse_talk_list),
            (server.parse_research_text, research.parse_research_text),
            (server.clean, values.clean),
            (server.company_key, talks.company_key),
            (server.match_loose, talk_sync.match_loose),
            (server.dedupe_items, talk_sync.dedupe_items),
            (server.items_needing_detail, talk_sync.items_needing_detail),
            (server.plan_cleanup, talk_sync.plan_cleanup),
        ]
        for got, want in pairs:
            self.assertIs(got, want, "%s 和 core 里的不是同一个对象" % got)


def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])

    class ProjectResult(unittest.TextTestResult):
        def _label(self, test):
            return test.shortDescription() or test.id().split(".")[-1]

        def addSuccess(self, test):
            super().addSuccess(test)
            self.stream.writeln("[PASS] " + self._label(test))

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self.stream.writeln("[FAIL] " + self._label(test))

        def addError(self, test, err):
            super().addError(test, err)
            self.stream.writeln("[FAIL] " + self._label(test) + "  （出错）")

    print("检查 core/ 的纯函数（不用开程序、不碰真实数据）\n")
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=0,
                                     resultclass=ProjectResult)
    result = runner.run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("\n共 %d 项，通过 %d，失败 %d" % (total, total - bad, bad))
    print("结果：" + ("全部通过" if result.wasSuccessful() else "存在失败项"))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
