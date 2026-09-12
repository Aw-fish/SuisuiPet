"""对话消息结构（接口格式与落盘格式之间的转换）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"


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
        """转成 OpenAI 兼容接口的 message 格式。"""
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
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
