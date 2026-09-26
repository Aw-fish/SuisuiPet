"""对话消息结构（接口格式与落盘格式之间的转换）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"

#: 回复被打断时追加到 assistant 内容末尾的标记，让模型知道自己被切断了。
#: 只加在**发给模型的上下文**里（见 :meth:`Message.to_api`），落盘与界面都保留干净正文。
INTERRUPT_MARK = "[被打断]"

#: 上下文里真的出现被打断的回复时，才解释这个标记——没被打断就不花这份 token
INTERRUPT_HINT = (
    f"若你上一轮回复的末尾出现 {INTERRUPT_MARK}，表示用户当时打断了你的上一句话，可以对此做出反应，或是直接回应用户的新消息"
)

#: 环境提示（目前只有时间）的旁白标记，挂在当轮消息前面。
#: 只进请求，和 INTERRUPT_MARK 一样不影响落盘与界面。
NARRATION_TAG = "[当前时间]"

#: 上下文里真的带旁白时才注入，边界说清楚——不然模型会把时间当成常驻任务反复提起
NARRATION_HINT = (
    f"- 以 {NARRATION_TAG} 开头的是当前对话发生的现实时间，不是用户说的话。无需刻意提起，在与时间有关的话题中作为参考（如上午好/今天是周末等）"
)

#: 模型"不想对事件开口"时的固定回复：本地捕获后不显示、也不触发对白框，
#: 但这一轮照样记进会话日志与开发者面板——它确实发生过。
SILENT_MARK = "（不做回应）"
#: 去掉标记自带的括号，用于容忍全角 / 半角混用
_SILENT_BODY = SILENT_MARK.strip("（）()")

#: 事件消息（用户操作）的标记说明。只在上下文里真的带事件时才注入：
#: 要说清"这不是用户当面对你说的话"，以及不想回应时可以怎么表示。
EVENT_HINT = (
    "- 以 [ ] 开头的是角色的动作或环境事件（如 [拖动]、[番茄钟]、[动作]、[记事板]），不是用户当面对你说的话。"
    f"可以顺着它回应一句，也可以不作回应：只输出 {SILENT_MARK}，不输出其他内容"
)


def is_silent(text: str) -> bool:
    """这段回复是不是"明确表示不作回应"。容忍全角 / 半角括号与首尾空白。"""
    return text.strip().strip("（）()").strip() == _SILENT_BODY


def silent_pending(text: str) -> bool:
    """这段（可能还没收完的）文本仍有可能变成"不作回应"吗？

    流式转发前用它判断：是的话就先按住不发，免得界面上闪出几个字又消失。
    """
    return _SILENT_BODY.startswith(text.strip().strip("（）()").strip()) or is_silent(text)


def now_stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Message:
    role: str
    content: str = ""
    ts: str = field(default_factory=now_stamp)
    interrupted: bool = False
    #: 挂在**这条消息**前面的时间旁白（``NARRATION_TAG``）。它是消息自己的属性、随会话落盘，
    #: 所以之后每一轮把历史发出去时它都还带着——模型不会因为"时间行挪到新消息上"而从
    #: 第二轮起就失去时间观念。什么时候挂由 :meth:`MemoryStore.append_user` 决定。
    narration: str = ""
    #: 这条消息是不是「用户操作事件」（[拖动] / [番茄钟] / [跟随] 这类，见 :data:`EVENT_HINT`）。
    #: 事件用 user 角色进请求（模型可以顺着它回应），但界面上不显示——只留给请求、会话
    #: 日志与开发者面板。
    event: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str = ""

    def to_api(self) -> dict[str, Any]:
        """转成 OpenAI 兼容接口的 message 格式。

        两处只在**请求里**存在的修饰（落盘与界面都看不到）：

        * 被打断的回复在末尾补 ``INTERRUPT_MARK``（模型才知道那是说到一半）；
        * ``narration`` 作为 ``NARRATION_TAG`` 旁白加在最前面（当前时间这类环境提示）。
        """
        content = self.content
        if self.interrupted:
            content = f"{content}{INTERRUPT_MARK}" if content.strip() else INTERRUPT_MARK
        if self.narration:
            content = f"{NARRATION_TAG} {self.narration}\n{content}"
        payload: dict[str, Any] = {"role": self.role, "content": content}
        if self.tool_calls:
            payload["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload

    def to_record(self) -> dict[str, Any]:
        """落盘用字典，空字段省略，便于人工查看。"""
        record: dict[str, Any] = {"ts": self.ts, "role": self.role, "content": self.content}
        if self.narration:
            record["narration"] = self.narration
        if self.interrupted:
            record["interrupted"] = True
        if self.event:
            record["event"] = True
        if self.tool_calls:
            record["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            record["tool_call_id"] = self.tool_call_id
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "Message":
        return cls(
            role=str(record.get("role", ROLE_USER)),
            content=str(record.get("content", "")),
            ts=str(record.get("ts", "")) or now_stamp(),
            interrupted=bool(record.get("interrupted")),
            narration=str(record.get("narration", "")),
            event=bool(record.get("event")),
            tool_calls=list(record.get("tool_calls") or []),
            tool_call_id=str(record.get("tool_call_id", "")),
        )
