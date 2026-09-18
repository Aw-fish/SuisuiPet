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
    f"若你上一轮回复的末尾出现 {INTERRUPT_MARK}，表示用户当时打断了你，"
    "那不是一句完整的话。可以顺着被打断的地方接着说，也可以直接回应用户的新消息，"
    "但不要重复已经说过的内容，也不要假装那句话已经说完了。"
)


def now_stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Message:
    role: str
    content: str = ""
    ts: str = field(default_factory=now_stamp)
    interrupted: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str = ""

    def to_api(self) -> dict[str, Any]:
        """转成 OpenAI 兼容接口的 message 格式。

        被打断的回复会在末尾补上 ``INTERRUPT_MARK``：模型拿到的是"说到一半就被掐了"
        的上下文，而不是一句看起来已经说完的话。
        """
        content = self.content
        if self.interrupted:
            content = f"{content}{INTERRUPT_MARK}" if content.strip() else INTERRUPT_MARK
        payload: dict[str, Any] = {"role": self.role, "content": content}
        if self.tool_calls:
            payload["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload

    def to_record(self) -> dict[str, Any]:
        """落盘用字典，空字段省略，便于人工查看。"""
        record: dict[str, Any] = {"ts": self.ts, "role": self.role, "content": self.content}
        if self.interrupted:
            record["interrupted"] = True
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
            tool_calls=list(record.get("tool_calls") or []),
            tool_call_id=str(record.get("tool_call_id", "")),
        )
