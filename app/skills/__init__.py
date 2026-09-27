"""Agent 技能：模型可以自主调用的工具。

技能实现在这里，编排（把 ``tools`` 发出去、执行调用、把结果回填）在
``app/conversation/service.py``：技能只管"说清自己是什么、怎么执行"。
"""

from __future__ import annotations

from typing import Any, Callable

from app.skills.base import Skill, ToolRegistry
from app.skills.play import PlaySkill
from app.skills.weather import WeatherSkill

__all__ = ["Skill", "ToolRegistry", "PlaySkill", "WeatherSkill", "build_registry"]


def build_registry(
    settings: dict[str, Any] | None = None,
    *,
    play: Callable[[list[int], int], str] | None = None,
) -> ToolRegistry:
    """按设置组装当前可用的技能。

    只有一个总开关：关掉时返回空注册表——**任何**工具的说明都不会随请求发出去，模型
    自然无从调用。``play`` 是界面提供的播放回调（演奏要用它把播放转到 GUI 线程），
    拿不到就不装演奏：装了也只是个只会回"现在演奏不了"的空壳。
    """
    config = (settings or {}).get("skills") or {}
    if not config.get("enabled", True):
        return ToolRegistry()
    skills: list[Skill] = [WeatherSkill()]
    if play is not None:
        skills.append(PlaySkill(play))
    return ToolRegistry(skills)
