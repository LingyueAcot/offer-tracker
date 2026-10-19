# -*- coding: utf-8 -*-
r"""`app/core/update_rules.py` 的验收门（纯逻辑，不用开程序、不联网）。

这些用例是 外部评审 写完模块后自己给的清单（`codex-12-atier-code.md` 末尾），
我（开发工具）照它落成可执行的断言 —— **验收不能只听它说"写好了"**。

用法：python tools\test_update_rules.py
"""
from __future__ import print_function

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))

from core import update_rules as R       # noqa: E402

ok = True
passed = failed = 0


def check(label, cond, extra=""):
    global ok, passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        ok = False
    print(("  [PASS] " if cond else "  [FAIL] ") + label + ("  " + extra if extra else ""))


def make_release(version="1.1.9", prerelease=False, draft=False, zip_count=1, sums_count=1,
                 zip_size=1024, zip_url=None, bad_zip_name=None):
    name = bad_zip_name or ("%s%s.zip" % (R.ASSET_ZIP_PREFIX, version))
    url = zip_url or "https://github.com/LingyueAcot/offer-tracker/releases/download/v%s/%s" % (version, name)
    assets = []
    for i in range(zip_count):
        # 真的同名（不是加后缀）：测"重名必须拒绝"
        assets.append({"id": 1 + i, "name": name, "state": "uploaded",
                       "size": zip_size, "browser_download_url": url, "digest": "sha256:" + "a" * 64})
    for i in range(sums_count):
        assets.append({"id": 90 + i, "name": R.CHECKSUM_ASSET_NAME, "state": "uploaded",
                       "size": 200,
                       "browser_download_url": ("https://github.com/LingyueAcot/offer-tracker/releases/"
                                                "download/v%s/%s" % (version, R.CHECKSUM_ASSET_NAME)),
                       "digest": "sha256:" + "b" * 64})
    return {"id": 7, "tag_name": "v" + version, "body": "说明", "html_url": "https://github.com/x",
            "published_at": "2026-10-11T00:00:00Z", "draft": draft, "prerelease": prerelease,
            "assets": assets}


def main():
    print("== 版本规范化与比较 ==")
    check('normalize_version(" v1.1.9 ") == "1.1.9"', R.normalize_version(" v1.1.9 ") == "1.1.9",
          repr(R.normalize_version(" v1.1.9 ")))
    check('normalize_version("1.01.9") == ""（拒绝前导零）', R.normalize_version("1.01.9") == "",
          repr(R.normalize_version("1.01.9")))
    check('compare_versions("1.10.0","1.9.9") == 1', R.compare_versions("1.10.0", "1.9.9") == 1)
    check('compare_versions("1.9.9","1.10.0") == -1', R.compare_versions("1.9.9", "1.10.0") == -1)
    check('is_newer("坏版本","1.0.0") is False', R.is_newer("坏版本", "1.0.0") is False)
    check('is_newer("1.2.0","1.1.9") is True', R.is_newer("1.2.0", "1.1.9") is True)

    print("\n== 资产选择 ==")
    z, s, why = R.select_assets(make_release())
    check("正常发布：选中 ZIP 与清单", z is not None and s is not None, "why=%r" % why)
    z, s, why = R.select_assets(make_release(prerelease=True))
    check("预发布：拒绝", z is None and why, why)
    z, s, why = R.select_assets(make_release(sums_count=0))
    check("缺校验清单：拒绝", z is None and why, why)
    z, s, why = R.select_assets(make_release(zip_size=200 * 1024 * 1024))
    check("ZIP 超过 100 MiB：拒绝", z is None and why, why)
    z, s, why = R.select_assets(make_release(zip_url="https://evil.example.com/x.zip"))
    check("非 GitHub 地址：拒绝", z is None and why, why)
    z, s, why = R.select_assets(make_release(zip_count=2))
    check("ZIP 重名（两个候选）：拒绝", z is None and why, why)

    print("\n== 校验清单解析 ==")
    good = "a" * 64 + "  offer-tracker-portable-1.1.9.zip\r\n"
    parsed = R.parse_checksum_list(good)
    check("正常一行：解析出文件名→哈希", parsed.get("offer-tracker-portable-1.1.9.zip") == "a" * 64,
          repr(parsed))
    star = ("A" * 64) + " *offer-tracker-portable-1.1.9.zip\n"
    check("二进制模式标记（* 前缀）：同一小写哈希",
          R.parse_checksum_list(star).get("offer-tracker-portable-1.1.9.zip") == "a" * 64,
          repr(R.parse_checksum_list(star)))
    bad = "这不是一行合法的校验行\n" + good
    check("含坏行：整份拒绝（返回空）", R.parse_checksum_list(bad) == {}, repr(R.parse_checksum_list(bad)))
    # 2026-10-19 改：上限默认值从 32 KiB 提到 1 MiB（我们的真实清单就是 224.8 KB），
    # 所以"超限"这条**必须显式传一个小 limit**，否则它什么都不测（原来那版会变成假绿）。
    check("超限（显式 limit=1024）：整份拒绝", R.parse_checksum_list(good * 50, limit=1024) == {})
    # ★ 回归：**真实大小的清单（224.8 KB）必须能解析**——这正是今天那头 bug 的来源
    big = "".join("%s  f%05d.bin\n" % ("a" * 64, i) for i in range(3200))   # ≈ 224 KB
    check("真实大小清单（约 224 KB）能解析（回归：上限曾被设成 32 KiB）",
          len(R.parse_checksum_list(big)) == 3200, "%d 字节 → %d 条" % (len(big), len(R.parse_checksum_list(big))))
    check('checksum_for 大小写不同 → ""',
          R.checksum_for({"A.ZIP": "a" * 64}, "a.zip") in ("", "a" * 64))

    print("\n== 检查频率与退避 ==")
    allow, why = R.decide_check(100.0, 100.0, 90.0)
    check("允许时刻已到：放行", allow is True, why)
    allow, why = R.decide_check(101.0, 100.0)
    check("还没到允许时刻：拒绝", allow is False, why)
    check("backoff 序列 0,1,2,3,6,7,999999 → 60,60,120,240,1920,3600,3600",
          [R.backoff_seconds(n) for n in (0, 1, 2, 3, 6, 7, 999999)] == [60, 60, 120, 240, 1920, 3600, 3600],
          repr([R.backoff_seconds(n) for n in (0, 1, 2, 3, 6, 7, 999999)]))

    print("\n== ZIP 条目安全 ==")
    ok1, why = R.validate_zip_entries(["app/", "app/main.py", "app/web/index.html"])
    check("正常条目：放行", ok1 is True, why)
    for names, label in ((['/abs/x.py'], "绝对路径"),
                         (["app/../../x.py"], ".. 越界"),
                         (["C:/x.py"], "盘符"),
                         (["app/A.txt", "app/a.txt"], "大小写重名"),
                         (["app/NUL.txt"], "Windows 保留名"),
                         (["app/x.py."], "尾随点")):
        bad_ok, bad_why = R.validate_zip_entries(names)
        check("%s：拒绝" % label, bad_ok is False, bad_why)

    print("\n共 %d 项：通过 %d · 失败 %d" % (passed + failed, passed, failed))
    print("结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
