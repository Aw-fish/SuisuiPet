"""时间文本化。

给模型看的"现在"、记忆条目里的日期都从这里出；将来做「取时间」工具时直接复用
这两个函数，不必再写一套格式。

这里只管把时间变成可读文本，不参与任何判断逻辑——权重衰减之类留在 memory.py。
"""

from __future__ import annotations

from datetime import datetime

#: 周一为 0，与 ``datetime.weekday()`` 对齐
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def now_text(moment: datetime | None = None) -> str:
    """给模型的当前时间，例如 ``2026-09-18（周五）09:30``。"""
    current = moment or datetime.now()
    return f"{current:%Y-%m-%d}（{WEEKDAYS[current.weekday()]}）{current:%H:%M}"


def day_text(ts: str, moment: datetime | None = None) -> str:
    """记忆条目的日期：同一年只说"9月12日"，跨年才补上年份。"""
    stamp = _parse(ts)
    if stamp is None:
        return ""
    if stamp.year == (moment or datetime.now()).year:
        return f"{stamp.month}月{stamp.day}日"
    return f"{stamp.year}年{stamp.month}月{stamp.day}日"
