# -*- coding: utf-8 -*-
"""
services/profile.py —— 个人画像（`个人画像.json`）的**读写**

2026-09-24 从 `server.py` 搬过来（S4）。调研域要读画像，而 `services/` 不许回头
import server，所以把这一小块先单独放这里。

2026-09-25 又拆了一次，**因为默认值里夹带了开发者的个人信息**：
  - 纯规则（默认值长什么样、关键词怎么派生、填完没有、显示文本）搬去 **`core/profile.py`**；
  - 本文件只剩「读 / 写 / 合并文件」。
  这样默认画像才能是**空白模板**，正式版发出去不带任何人的专业和城市。

【行为（与搬之前逐字一致，只多了写入能力）】
- 文件不存在 -> **顺手写一份空白默认画像**（提示用户去填），并返回默认值；
- 文件存在且是 JSON 对象 -> 与默认值**合并**（用户只写了几个键也不会缺字段）；
- 文件坏了 / 不是对象 -> 静默回落默认值（**绝不因为画像坏了让程序起不来**）。

【规矩】
- 路径由 `server.py` 用 `use()` 注入（唯一来源仍是那边的 `PROFILE_PATH`）。
- 用户自己维护这个文件 —— 除了"不存在时生成一份默认的"和"他在界面上点保存"，这里不主动改它。
"""

import json
import os

from core.profile import (DEFAULT_PROFILE, derive_major_keywords,  # noqa: F401
                          normalize_profile, profile_needs_setup, profile_summary,
                          split_major)

# 画像文件路径。server.py 启动时注入。
_profile_path = {"value": ""}


def use(profile_path):
    """server.py 启动时调一次，把画像路径装进来。"""
    _profile_path["value"] = profile_path


def path():
    """当前画像路径（自检用）。"""
    return _profile_path["value"]


def load_profile():
    """
    读取个人画像；没有就按**空白默认值**生成一份，方便用户在界面上填。

    返回的一定是**字段齐全**的画像（见 `core.profile.normalize_profile`）。
    """
    target = _profile_path["value"]
    if not target or not os.path.isfile(target):
        _write(DEFAULT_PROFILE)
        return normalize_profile({})
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return normalize_profile(data)
    except Exception:
        pass
    return normalize_profile({})


def save_profile(payload):
    """
    把界面上填的画像写回文件。**只接受白名单里的键**，其余一律忽略。

    为什么要有白名单：这个接口是页面调的，而返回值会被 `build_state()` 带回前端。
    不做限制的话，前端可以往画像里塞任意键、越塞越大。白名单同时也保证
    「画像文件长什么样」这件事是**代码说了算**，不是调用方说了算。

    写入方式：先写 `.tmp` 再 `os.replace` —— 中途失败也不会留半截文件
    （和 `backup.py` 的"要么完整要么不存在"同一个态度）。

    返回 `(写好的画像, 错误信息)`，错误信息为 None 表示成功。
    """
    if not isinstance(payload, dict):
        return None, "画像内容格式不对"

    current = load_profile()

    # 用户能改的字段（其余键忽略）
    editable_text = ("name", "degree", "graduate_year",
                     "major_primary", "major_secondary")
    editable_list = ("major_keywords", "city_keywords", "target_positions",
                     "target_industries", "position_keywords",
                     "plus_keywords", "minus_keywords")

    merged = dict(current)
    for key in editable_text:
        if key in payload:
            merged[key] = str(payload.get(key) or "").strip()
    for key in editable_list:
        if key in payload:
            merged[key] = payload.get(key)

    # ⚠️ 「主专业必填」必须在 normalize **之前**判，而且要看**这次提交的值**，
    #    不能看合并后的结果 —— 合并会把已存的主专业填回来，
    #    于是「用户想清空主专业」这件事被静默放行（自测时真踩到这个）。
    submitted_major = str(merged.get("major_primary") or "").strip()
    if not submitted_major:
        return None, "请先填写主专业（必填）"

    clean = normalize_profile(merged)
    # 关键词：用户显式传了就用他的；没传、或者传了空，就按专业重新派生。
    # （重新派生在这里是**正确**的：主专业这次一定非空，派生结果一定有意义。）
    if not clean.get("major_keywords"):
        clean["major_keywords"] = derive_major_keywords(
            clean.get("major_primary"), clean.get("major_secondary"))
    clean["major"] = str(clean.get("major_primary") or "").strip()

    if not _write(clean):
        return None, "画像文件写不进去（磁盘只读或没有权限）"
    return clean, None


def profile_status():
    """
    给界面判断"要不要引导用户去填画像"用的信号（2026-09-25 加）。

    返回 `{"filled": bool, "needs_setup": bool, "summary": {...}}`。
    前端首页据此显示那条"第一次用，先花一分钟填一下"的引导。
    """
    p = load_profile()
    needs = profile_needs_setup(p)
    return {"filled": not needs, "needs_setup": needs, "summary": profile_summary(p)}


def _write(data):
    """把画像写成 JSON。先写临时文件再替换，避免留半截文件。"""
    target = _profile_path["value"]
    if not target:
        return False
    try:
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        return True
    except Exception:
        return False
