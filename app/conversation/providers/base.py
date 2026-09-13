"""LLM Provider 抽象。

界面层不直接依赖任何 HTTP 细节，只跟 :class:`~app.conversation.service.ConversationService`
打交道；换服务商时只要再实现一个 Provider 即可。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, Sequence


@dataclass
class ToolCall:
    """模型请求调用某个工具（P2 才会真正执行）。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Chunk:
    """一次流式增量：要么是文本片段，要么是收尾时的工具调用汇总。"""

    delta: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finished: bool = False


class ProviderError(RuntimeError):
    """请求失败，文案可以直接展示给用户。"""


class LLMProvider(Protocol):
    def stream(
        self,
        messages: Sequence[dict],
        tools: Sequence[dict] | None = None,
        cancel: threading.Event | None = None,
    ) -> Iterator[Chunk]:
        """流式产出增量；``cancel`` 置位后应尽快结束。"""
        ...

    def complete(
        self,
        messages: Sequence[dict],
        max_tokens: int = 800,
        timeout: int = 40,
        temperature: float | None = None,
    ) -> str:
        """非流式一次拿回完整回复，记忆抽取这类一次性任务用。"""
        ...

    def abort(self) -> None:
        """从其它线程强制中断正在进行的请求。"""
        ...
