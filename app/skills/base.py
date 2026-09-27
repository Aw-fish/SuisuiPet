"""Agent 技能：模型可以自己调用的工具。

一个技能 = 给模型看的说明（``name`` / ``description`` / ``parameters``）+ 本地真正
执行它的 :meth:`Skill.run`。说明随每次请求发给模型，模型据此决定要不要调用、传什么
参数；本地执行完把结果原样回填，让它接着把话说完。

两条硬规矩：

* 技能跑在**请求线程**里，绝不能碰 Qt（界面信号才是回主线程的东西）；
* 技能抛出的异常不许逃出去——一律变成一句可读的失败说明交回模型，由它告诉用户
  "没查到"，比把栈丢到界面上强。
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from app import devtools


@runtime_checkable
class Skill(Protocol):
    """一个可被模型调用的技能。"""

    #: 工具名。给模型看的，用英文小写下划线更像函数名
    name: str
    #: 什么时候该用它、参数怎么给。模型只看得到这一段，所以写清楚
    description: str
    #: JSON Schema 形式的参数说明
    parameters: dict[str, Any]
    #: 执行期间给用户看的一行字（如"查天气"）
    label: str

    def run(self, arguments: dict[str, Any]) -> str:
        """执行并返回一段**给模型看**的文本结果。"""
        ...


class ToolRegistry:
    """当前可用的技能集合：转成接口要的 ``tools`` 定义，并按名字执行。"""

    def __init__(self, skills: Sequence[Skill] = ()) -> None:
        self._skills: dict[str, Skill] = {skill.name: skill for skill in skills}

    def __len__(self) -> int:
        return len(self._skills)

    def definitions(self) -> list[dict[str, Any]]:
        """转成 OpenAI 兼容接口的 ``tools`` 数组。没有技能时返回空列表。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": skill.name,
                    "description": skill.description,
                    "parameters": skill.parameters,
                },
            }
            for skill in self._skills.values()
        ]

    def label_for(self, name: str) -> str:
        """技能执行期间显示给用户的那行字；名字对不上就退回工具名。"""
        skill = self._skills.get(name)
        return skill.label if skill is not None else name

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        """执行一个技能；任何异常都变成可读的失败说明，绝不让它逃进请求线程。"""
        skill = self._skills.get(name)
        if skill is None:
            devtools.log.warning("技能 · 模型调用了不存在的工具：%s", name)
            return f"没有名为 {name} 的工具，请只用已提供的工具。"
        try:
            return str(skill.run(arguments or {}))
        except Exception as exc:  # noqa: BLE001 - 技能出错不该拖垮这一轮对话
            devtools.log.warning("技能 · %s 执行失败：%s", name, exc, exc_info=True)
            return f"{name} 执行失败：{exc}"
