# -*- coding: utf-8 -*-
r"""schema_compat.py —— **数据库兼容判据**（S / R / M）。

背景（方案 §3.5.1 第四节，外部评审 复审第 1 条把原来那版完全推翻）：
判断"这个库能不能被这份程序打开"**不能比两个程序常量**（目标包的 `SCHEMA_VERSION` vs 本地程序的
`SCHEMA_VERSION`）—— 因为库里的版本记录**以前不会随迁移更新**（`schema.py` 写的是 `INSERT OR IGNORE`，
老库那条记录一直存在，迁移完还是旧值），所以拿常量比会**既拦住正常迁移、又放过"旧程序开新库"**。
那处 `INSERT OR IGNORE` 已经改成 UPSERT（迁移成功后把真实版本写回库里），这条判据才有意义。

三个概念：

| 记号 | 含义 |
|---|---|
| `S` | **库里真实记录的版本**（从库里读，不是程序常量） |
| `R` | 这份程序**直接兼容**的库版本集合 |
| `M` | 这份程序**有已验证迁移路径**的旧库版本集合 |

判据：

| 情形 | 处置 |
|---|---|
| `S ∈ R ∪ M` | 放行（迁移由 `schema.py` 做） |
| `S` 比支持的高（例如从新版退回来的库） | **拒绝**，在没认清之前**不做任何业务写入** |
| `S` 缺失（很老的库 / 全新库） | 当作 `M`（走迁移/建库），记一条说明 |
| `S` 是别的值 | **拒绝**（未知格式，宁可不动） |

检查在**任何迁移/业务写入之前**做（`server.py` 的 `main()` 里，备份之后、`init_db()` 之前）。
更新器自己不打开数据库 —— 它只负责换程序文件。
"""
from __future__ import print_function

# R：这份程序直接兼容的库版本
SUPPORTED = frozenset([2])
# M：有已验证迁移路径的旧库版本（schema.py 会把它们补齐到 SUPPORTED）
MIGRATABLE = frozenset([1])


def check(current, supported=None, migratable=None):
    """**纯函数**：给"库里读到的版本"，返回 `(ok, reason)`。

    `current` 为 None 表示库里没有这条记录（很老的库或全新库）。
    """
    supported = SUPPORTED if supported is None else supported
    migratable = MIGRATABLE if migratable is None else migratable
    if current is None:
        return True, "库里没有版本记录（很老的库或全新库）→ 按可迁移处理"
    try:
        value = int(current)
    except (TypeError, ValueError):
        return False, "库里的版本记录不是整数（%r）→ 无法识别，拒绝打开" % (current,)
    if value in supported:
        return True, "库版本 %d 直接兼容" % value
    if value in migratable:
        return True, "库版本 %d 有已验证的迁移路径" % value
    if value > max(supported | migratable):
        return False, ("库版本 %d 比这份程序支持的（%s）还新 —— "
                       "多半是用了更新版程序建的库；**拒绝打开**，免得写坏它"
                       % (value, "/".join(str(v) for v in sorted(supported | migratable))))
    return False, "库版本 %d 不认识（支持 %s）" % (value, "/".join(str(v) for v in sorted(supported | migratable)))


def describe(current):
    ok, reason = check(current)
    return "库版本记录=%s；%s：%s" % (current, "放行" if ok else "拒绝", reason)
