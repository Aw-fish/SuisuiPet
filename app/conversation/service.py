"""对话编排：组装上下文 → 调用 provider → 流式回调 → 落盘记忆。

界面只跟 :class:`ConversationService` 打交道：

* 发消息用 :meth:`send`，随时可以用 :meth:`interrupt` 打断；
* 通过 ``delta`` / ``sentence`` / ``finished`` / ``failed`` / ``busy_changed`` 信号回推。

网络请求跑在 :class:`QThread` 里，主线程不会被阻塞。
"""

from __future__ import annotations

import threading
from typing import Any, Sequence

from PySide6.QtCore import QObject, QThread, Signal

from app.conversation.consolidation import consolidate_pending
from app.conversation.memory import MemoryStore
from app.conversation.message import Message, ROLE_ASSISTANT, ROLE_USER
from app.conversation.providers.base import LLMProvider, ProviderError
from app.conversation.providers.openai_compat import OpenAICompatProvider
from app.conversation.sentence import SentenceSplitter

DEFAULT_LIMIT = 20
DEFAULT_TEMPERATURE = 0.8
DEFAULT_TIMEOUT = 60


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def build_provider(info: dict) -> LLMProvider | None:
    """按角色配置构造 provider，缺少必要项时返回 None。"""
    base_url = str(info.get("base_url", "")).strip()
    model = str(info.get("model", "")).strip()
    if not base_url or not model:
        return None
    return OpenAICompatProvider(
        base_url=base_url,
        model=model,
        api_key=str(info.get("api_key", "")).strip(),
        temperature=_as_float(info.get("temperature"), DEFAULT_TEMPERATURE),
        timeout=int(_as_float(info.get("timeout"), DEFAULT_TIMEOUT)),
        proxy=str(info.get("proxy", "")).strip(),
    )


def check_connection(info: dict) -> tuple[bool, str]:
    """「测试连接」用：发一条极短请求，把报错直接翻译成可读文案。"""
    if not str(info.get("base_url", "")).strip():
        return False, "没有配置 API 地址"
    if not str(info.get("model", "")).strip():
        return False, "没有配置模型名称"
    if not str(info.get("api_key", "")).strip():
        return False, "还没有填写 API Key"
    provider = build_provider(info)
    if provider is None:
        return False, "配置不完整"
    try:
        model = provider.probe()  # type: ignore[attr-defined]
    except ProviderError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - 网络层异常统一兜住
        return False, f"测试失败：{exc}"
    return True, f"连接成功，模型 {model} 可用"


class _StreamWorker(QThread):
    """在工作线程里跑一次流式请求。"""

    delta = Signal(str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, provider: LLMProvider, messages: Sequence[dict], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._provider = provider
        self._messages = list(messages)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()
        self._provider.abort()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        parts: list[str] = []
        try:
            for chunk in self._provider.stream(self._messages, cancel=self._cancel):
                if self._cancel.is_set():
                    break
                if chunk.delta:
                    parts.append(chunk.delta)
                    self.delta.emit(chunk.delta)
        except ProviderError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 线程边界统一兜住
            self.failed.emit(f"请求出错：{exc}")
            return
        self.finished.emit("".join(parts))


class ConsolidationWorker(QThread):
    """在后台整理记忆：抽取事实 / 偏好 / 事件 / 约定并合并摘要。"""

    done = Signal(int, int)
    failed = Signal(str)

    def __init__(self, store: MemoryStore, info: dict, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._info = dict(info)

    def run(self) -> None:
        provider = build_provider(self._info)
        if provider is None:
            self.failed.emit("没有配置 API 地址或模型名称，无法整理记忆")
            return
        try:
            added, merged = consolidate_pending(self._store, provider)
        except ProviderError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 线程边界统一兜住
            self.failed.emit(f"整理记忆失败：{exc}")
            return
        self.done.emit(added, merged)


class ConversationService(QObject):
    delta = Signal(str)
    sentence = Signal(str)
    finished = Signal(str)
    failed = Signal(str)
    busy_changed = Signal(bool)
    consolidated = Signal(int, int)
    consolidation_failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._character = ""
        self._info: dict = {}
        self._base_prompt = ""
        self._memory: MemoryStore | None = None
        self._worker: _StreamWorker | None = None
        self._consolidator: ConsolidationWorker | None = None
        self._splitter = SentenceSplitter()

    # ---- 配置 ---------------------------------------------------------------

    def configure(self, character: str, info: dict, base_prompt: str = "") -> None:
        """切换当前角色：换角色同时切换记忆目录。

        ``base_prompt`` 是与角色无关的通用说话约束（全局设置），发请求时拼在
        角色提示词前面。
        """
        self._info = dict(info or {})
        self._base_prompt = base_prompt or ""
        if character != self._character:
            self._character = character
            self._memory = MemoryStore(character) if character else None

    @property
    def character(self) -> str:
        return self._character

    @property
    def memory(self) -> MemoryStore | None:
        return self._memory

    @property
    def busy(self) -> bool:
        return self._worker is not None

    # ---- 会话 ---------------------------------------------------------------

    def history(self, limit: int = 40) -> list[Message]:
        if self._memory is None:
            return []
        return self._memory.load(limit)

    def start_new_session(self) -> None:
        if self._memory is not None:
            self._memory.new_session()

    def reset_memory(self) -> int:
        """清空当前角色的全部记忆与对话记录，并另起一段新会话。"""
        if self._memory is None:
            return 0
        removed = self._memory.reset()
        self._memory.new_session()
        return removed

    @property
    def consolidating(self) -> bool:
        return self._consolidator is not None

    def start_consolidation(self) -> None:
        """后台整理所有待处理会话。幂等，可安全重复调用。"""
        if self._memory is None or self._consolidator is not None:
            return
        worker = ConsolidationWorker(self._memory, self._info, self)
        worker.done.connect(self._on_consolidated)
        worker.failed.connect(self._on_consolidation_failed)
        worker.finished.connect(self._on_consolidation_finished)
        worker.finished.connect(worker.deleteLater)
        self._consolidator = worker
        worker.start()

    def _on_consolidated(self, added: int, merged: int) -> None:
        self.consolidated.emit(added, merged)

    def _on_consolidation_failed(self, message: str) -> None:
        self.consolidation_failed.emit(message)

    def _on_consolidation_finished(self) -> None:
        self._consolidator = None

    def send(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self.busy:
            self.failed.emit("上一条还在生成中，先停止或等它说完")
            return
        if self._memory is None:
            self.failed.emit("还没有选择角色")
            return
        provider = build_provider(self._info)
        if provider is None:
            self.failed.emit("没有配置 API 地址或模型名称，请到设置 → 角色设置里填写")
            return
        self._memory.append(Message(role=ROLE_USER, content=text))
        # 把用户这句话交给记忆检索，召回与当前话题相关的长期记忆
        messages = self._memory.build_context(self._system_prompt(), self._context_limit(), text)
        self._splitter.reset()
        worker = _StreamWorker(provider, messages, self)
        worker.delta.connect(self._on_delta)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        self._worker = worker
        self.busy_changed.emit(True)
        worker.start()

    def interrupt(self) -> None:
        """打断当前生成；已生成的部分会保留并标记 interrupted。"""
        worker = self._worker
        if worker is not None:
            worker.cancel()

    # ---- 内部 ---------------------------------------------------------------

    def _on_delta(self, text: str) -> None:
        self.delta.emit(text)
        for sentence in self._splitter.feed(text):
            self.sentence.emit(sentence)

    def _on_finished(self, text: str) -> None:
        self._finalize(text)

    def _on_failed(self, message: str) -> None:
        self._finalize("", error=message)

    def _finalize(self, text: str, error: str = "") -> None:
        worker = self._worker
        self._worker = None
        interrupted = bool(worker is not None and worker.cancelled)
        if self._memory is not None and (text or interrupted):
            self._memory.append(
                Message(role=ROLE_ASSISTANT, content=text, interrupted=interrupted)
            )
        self.busy_changed.emit(False)
        if error:
            self.failed.emit(error)
        else:
            self.finished.emit(text)

    def _system_prompt(self) -> str:
        """通用的说话约束在前，角色自己的设定在后，两者拼成一条 system。"""
        parts = [
            str(self._base_prompt).strip(),
            str(self._info.get("system_prompt", "")).strip(),
        ]
        return "\n\n".join(part for part in parts if part)

    def _context_limit(self) -> int:
        try:
            return max(2, min(100, int(self._info.get("max_context_messages", DEFAULT_LIMIT))))
        except (TypeError, ValueError):
            return DEFAULT_LIMIT
