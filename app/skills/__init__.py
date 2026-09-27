"""Agent 技能：模型可以自主调用的工具。

技能实现在这里，编排（把 ``tools`` 发出去、执行调用、把结果回填）在
``app/conversation/service.py``：技能只管"说清自己是什么、怎么执行"。
"""

from __future__ import annotations

from typing import Any, Callable

from app.skills.base import Skill, ToolRegistry
from app.skills.sing import SingSkill
from app.skills.weather import WeatherSkill

__all__ = ["Skill", "ToolRegistry", "SingSkill", "WeatherSkill", "build_registry"]


def build_registry(
    settings: dict[str, Any] | None = None,
    *,
    sing: Callable[[list[int], int], str] | None = None,
) -> ToolRegistry:
    """按设置组装当前可用的技能。

    关掉的技能不进注册表——它的说明连请求都不会发出去，模型自然无从调用。
    ``sing`` 是界面提供的播放回调（唱歌技能要用它把播放转到 GUI 线程），拿不到就不装
    唱歌：装了也是个只会回"现在唱不了"的空壳。
    """
    config = (settings or {}).get("skills") or {}
    skills: list[Skill] = []
    weather = config.get("weather") or {}
    if weather.get("enabled", True):
        skills.append(WeatherSkill())
    singing = config.get("sing") or {}
    if singing.get("enabled", True) and sing is not None:
        skills.append(SingSkill(sing))
    return ToolRegistry(skills)
