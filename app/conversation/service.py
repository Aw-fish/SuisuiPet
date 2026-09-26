"""对话编排：组装上下文 → 调用 provider → 流式回调 → 落盘记忆。

界面只跟 :class:`ConversationService` 打交道：

* 发消息用 :meth:`send`，随时可以用 :meth:`interrupt` 打断；
* 通过 ``delta`` / ``sentence`` / ``finished`` / ``failed`` / ``busy_changed`` 信号回推。

网络请求跑在 :class:`QThread` 里，主线程不会被阻塞。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Sequence

from PySide6.QtCore import QObject, QThread, Signal

from app import devtools
from app.conversation import prompts
from app.conversation.consolidation import consolidate_pending
from app.conversation.memory import MemoryStore
from app.conversation.message import (
    SILENT_MARK,
    Message,
    ROLE_ASSISTANT,
    ROLE_USER,
    is_silent,
    silent_pending,
)
from app.conversation.providers.base import LLMProvider, ProviderError
from app.conversation.providers.openai_compat import OpenAICompatProvider
from app.conversation.sentence import SentenceSplitter
from app.pet.emotion import EXPRESSION_HINT, TagFilter, mood_asset

DEFAULT_LIMIT = 20
DEFAULT_TEMPERATURE = 0.8
DEFAULT_TIMEOUT = 60
#: 各事件的冷却秒数（没列出的事件不冷却，来了就发）。
#: 只拦会反复触发的：拖动每次松手都可能算一次，记事板每敲几下就写一次——每个事件都是
#: 一次真实请求，不拦一下会烧掉大量调用。番茄钟与动作模式是明确的单次动作，冷却反而会
#: 让"开始专注 → 十分钟后暂停"这种前后关系断掉。
EVENT_COOLDOWN = {"拖动": 20, "记事板": 10}


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


#: 推理内容转发给开发者面板的节奏：攒够这么多字符、或距上次超过这么久，就发一次。
#: 推理动辄上千个片段，逐个发会淹掉主线程——面板那边也刷不过来。
REASONING_FLUSH_CHARS = 120
REASONING_FLUSH_SECONDS = 0.2


class _StreamWorker(QThread):
    """在工作线程里跑一次流式请求。"""

    delta = Signal(str)
    #: True = 收到推理内容（还在想），False = 正文开始了（开始说）
    reasoning_changed = Signal(bool)
    #: 限频转发出去的推理片段（只喂开发者面板，用来实时长出来）
    reasoning_delta = Signal(str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, provider: LLMProvider, messages: Sequence[dict], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._provider = provider
        self._messages = list(messages)
        self._cancel = threading.Event()
        #: 这一轮攒下的推理内容（``reasoning_content``），收尾时交给开发者面板。
        #: 推理片段动辄上千个，不逐个发信号，只在这里累加。
        self.reasoning = ""

    def cancel(self) -> None:
        self._cancel.set()
        self._provider.abort()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        parts: list[str] = []
        error = ""
        reasoning = False
        speaking = False
        pending: list[str] = []
        last_flush = time.monotonic()

        def flush_reasoning() -> None:
            """把攒下的推理片段一次性交给面板（它那边实时往折叠块里追加）。"""
            nonlocal last_flush
            if pending:
                self.reasoning_delta.emit("".join(pending))
                pending.clear()
            last_flush = time.monotonic()

        try:
            for chunk in self._provider.stream(self._messages, cancel=self._cancel):
                if self._cancel.is_set():
                    break
                if chunk.reasoning:
                    self.reasoning += chunk.reasoning
                    pending.append(chunk.reasoning)
                    if (
                        sum(len(item) for item in pending) >= REASONING_FLUSH_CHARS
                        or time.monotonic() - last_flush >= REASONING_FLUSH_SECONDS
                    ):
                        flush_reasoning()
                # 只在状态真的翻转时发信号：推理片段动辄上千个，逐个发会淹掉主线程
                if chunk.reasoning and not reasoning:
                    reasoning = True
                    speaking = False
                    self.reasoning_changed.emit(True)
                if chunk.delta:
                    # 没有推理阶段的模型直接进正文，这里同样要通知一次（转成"说话"）
                    if not speaking:
                        speaking = True
                        reasoning = False
                        self.reasoning_changed.emit(False)
                    parts.append(chunk.delta)
                    self.delta.emit(chunk.delta)
        except ProviderError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 线程边界统一兜住
            error = f"请求出错：{exc}"
        # 收尾前把尾巴发出去，免得最后不满一个节流窗口的那几句卡在缓冲里
        flush_reasoning()
        # 主动打断会把阻塞中的连接掐断，读取随之抛异常——那是预期行为，不是失败。
        # 这里一律按正常结束交回去，由上层以「被打断」收尾并保留已生成的部分。
        if self._cancel.is_set():
            self.finished.emit("".join(parts))
            return
        if error:
            self.failed.emit(error)
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
    #: True = 模型还在推理（只吐 reasoning_content），False = 正文开始流出来了
    reasoning_changed = Signal(bool)
    consolidated = Signal(int, int)
    consolidation_failed = Signal(str)
    #: 回复里标出的表情（立绘素材名，None 表示回到默认立绘）
    mood_changed = Signal(object)
    #: 这一轮模型明确表示"不作回应"（SILENT_MARK）：界面不显示，但历史与日志里留着
    silenced = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._character = ""
        self._info: dict = {}
        self._base_prompt = ""
        self._memory: MemoryStore | None = None
        self._worker: _StreamWorker | None = None
        self._consolidator: ConsolidationWorker | None = None
        self._splitter = SentenceSplitter()
        self._tag_filter = TagFilter()
        self._clean: list[str] = []
        #: 本轮从回复里剥掉了多少个表情标记（只给开发者面板看）
        self._tag_count = 0
        self._auto_expression = False
        #: 本次请求的时间基准，用来算首字延迟与总耗时（只给开发者面板看）
        self._started_at: float | None = None
        self._first_delta_at: float | None = None
        #: 已经放给下游的正文长度（见 _emit_visible：正文前缀要先按住看是不是 SILENT_MARK）
        self._emitted = 0
        #: 正文是不是已经开始了，但还没找到机会放出来（"思考 → 说话"的切换就此推迟）
        self._talk_pending = False
        #: 每类事件上次转发的时间，用于冷却（只影响事件，不影响正常对话）
        self._event_at: dict[str, float] = {}

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

    def set_auto_expression(self, enabled: bool) -> None:
        """开启自动表情：提示词里追加表情标记说明，并按标记切换立绘表情。"""
        self._auto_expression = bool(enabled)

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
        devtools.log.info("记忆 · 整理完成：新增 %d 条，合并 %d 条", added, merged)
        devtools.record("consolidated", added=added, merged=merged)
        self.consolidated.emit(added, merged)

    def _on_consolidation_failed(self, message: str) -> None:
        devtools.log.warning("记忆 · 整理失败：%s", message)
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
        provider = self._provider()
        if provider is None:
            return
        self._start(provider, self._memory.append_user(text), query=text)

    def notify_event(self, tag: str, text: str = "") -> bool:
        """把一次用户操作（拖动 / 番茄钟 / 跟随）作为**事件**交给模型，返回是否发出去了。

        事件与对话的区别：模型可以顺着它说一句，也可以不作回应（输出
        :data:`SILENT_MARK`，本地捕获后界面上什么都不显示）。两种情况下直接丢掉：

        * 上一轮还在生成——事件是"刚刚发生"的事，排队只会让模型面对一串过期动作；
        * :data:`EVENT_COOLDOWN` 里列了冷却的事件（拖动、记事板）在该秒数内已经发过。
        """
        if self._memory is None or self.busy:
            return False
        now = time.monotonic()
        cooldown = EVENT_COOLDOWN.get(tag, 0)
        if cooldown and now - self._event_at.get(tag, 0.0) < cooldown:
            return False
        provider = self._provider(report=False)
        if provider is None:
            return False
        self._event_at[tag] = now
        label = f"[{tag}] {text}".strip()
        devtools.log.info("事件 · %s（交给模型，可回应可不回应）", label)
        self._start(provider, self._memory.append_event(label), query="")
        return True

    def _provider(self, report: bool = True) -> LLMProvider | None:
        """取出这一轮要用的 provider；没配好就返回 None。

        ``report`` 为假时只写日志、不发失败信号——事件不该因为没配 API 就弹个错误出来。
        """
        provider = build_provider(self._info)
        if provider is not None:
            return provider
        devtools.log.warning("对话 · 无法发送：没有配置 API 地址或模型名称")
        if report:
            self.failed.emit("没有配置 API 地址或模型名称，请到设置 → 角色设置里填写")
        return None

    def _start(self, provider: LLMProvider, user_message: Message, query: str) -> None:
        """把一轮请求真正发出去：重置状态 → 记录参数 → 组装上下文 → 起线程。"""
        assert self._memory is not None          # 两个调用方都先查过
        self._tag_filter.reset()
        self._clean.clear()
        self._tag_count = 0
        self._emitted = 0
        self._talk_pending = False
        # 参数先记录、再组装上下文：面板按发生顺序铺开，卡片头要排在"这一轮发了什么"前面
        devtools.record(
            "request",
            character=self._character,
            model=str(self._info.get("model", "")),
            temperature=self._info.get("temperature"),
            context_limit=self._context_limit(),
            # 这一轮新挂了时间行（面板据此在分隔线上标一句，说明从这里重新"对表"）
            narration=user_message.narration,
            # 事件轮：这条不是用户说的话，面板要区分开
            event=user_message.event,
        )
        # 把这一轮的内容交给记忆检索，召回与当前话题相关的长期记忆
        messages = self._memory.build_context(
            self._system_prompt(), self._context_limit(), query, self._runtime_notes()
        )
        self._started_at = time.monotonic()
        self._first_delta_at = None
        tokens = sum(devtools.estimate_tokens(str(item.get("content", ""))) for item in messages)
        devtools.log.info(
            "对话 · 发出请求（%s，上下文 %d 条 / 约 %d token）",
            self._character or "未选角色",
            len(messages),
            tokens,
        )
        self._splitter.reset()
        worker = _StreamWorker(provider, messages, self)
        worker.delta.connect(self._on_delta)
        worker.reasoning_changed.connect(self._on_reasoning_changed)
        worker.reasoning_delta.connect(self._on_reasoning_delta)
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
            devtools.log.info("对话 · 打断生成")
            worker.cancel()

    # ---- 内部 ---------------------------------------------------------------

    def _on_delta(self, text: str) -> None:
        if self._first_delta_at is None:
            self._first_delta_at = time.monotonic()
        # 先在流上剥掉情绪标记，下游（界面 / 落盘 / 句级切分）拿到的都是干净正文
        clean, tags = self._tag_filter.feed(text)
        self._tag_count += len(tags)
        for tag in tags:
            self.mood_changed.emit(mood_asset(tag))
        if not clean:
            return
        self._clean.append(clean)
        self._emit_visible()

    def _emit_visible(self) -> None:
        """把已累计的正文里"确定不是「不作回应」"的部分放给下游。

        ``SILENT_MARK`` 是逐字流进来的：直接转发会让界面先闪出"（不做"再消失，
        所以前几个字先按住，确认它不可能凑成这个标记之后再放行（顺带让桌宠的 Talk
        动作也不会为一次"不作回应"抖一下）。
        """
        text = "".join(self._clean)
        if silent_pending(text):
            return
        tail = text[self._emitted:]
        if not tail:
            return
        self._emitted = len(text)
        if self._talk_pending:
            # 正文真的开始了，这时候才把桌宠从"思考"切到"说话"
            self._talk_pending = False
            devtools.preview("reasoning_done")
            self.reasoning_changed.emit(False)
        self.delta.emit(tail)
        for sentence in self._splitter.feed(tail):
            self.sentence.emit(sentence)

    def _on_reasoning_changed(self, reasoning: bool) -> None:
        if reasoning:
            self.reasoning_changed.emit(True)
            return
        # 正文开始：先记着，等真有可展示的文字时再放出去（见 _emit_visible）
        self._talk_pending = True

    def _on_reasoning_delta(self, text: str) -> None:
        """推理内容的限频预览：只喂开发者面板，不进记忆、也不进对话界面。"""
        devtools.preview("reasoning", text=text)

    def _on_finished(self, text: str) -> None:
        # worker 回传的是原始全文（还带着标记），所以用自己累计的干净文本收尾；
        # 原始那份留给开发者面板对照——能看出模型究竟标了什么、又被剥掉了什么。
        tail, tags = self._tag_filter.flush()
        self._tag_count += len(tags)
        if tail:
            self._clean.append(tail)
        self._finalize("".join(self._clean), raw=text)

    def _on_failed(self, message: str) -> None:
        worker = self._worker
        if worker is not None and worker.cancelled:
            # 打断过程中冒出来的读取异常：按「被打断」收尾，别丢掉已经生成的内容
            self._finalize("".join(self._clean))
            return
        self._finalize("", error=message)

    def _finalize(self, text: str, error: str = "", raw: str = "") -> None:
        worker = self._worker
        self._worker = None
        interrupted = bool(worker is not None and worker.cancelled)
        #: 推理内容也算这一次的产出（推理模型可能几千 token 都在这里）
        reasoning = worker.reasoning if worker is not None else ""
        # 模型明确表示"不作回应"（事件轮常见）：界面什么都不显示，但这一轮照样留在
        # 历史与日志里——上下文不断档，翻日志也看得出它看到了什么、选择了什么
        silent = not error and not interrupted and is_silent(text)
        if silent:
            text = ""
        if self._memory is not None and (text or interrupted or silent):
            self._memory.append(
                Message(
                    role=ROLE_ASSISTANT,
                    content=SILENT_MARK if silent else text,
                    interrupted=interrupted,
                )
            )
        first = total = 0.0
        if self._started_at is not None:
            total = time.monotonic() - self._started_at
            if self._first_delta_at is not None:
                first = self._first_delta_at - self._started_at
        self._started_at = None
        self._first_delta_at = None
        devtools.record(
            "reply",
            text=text,
            raw=raw,
            reasoning=reasoning,
            tags=self._tag_count,
            silent=silent,
            interrupted=interrupted,
            error=error,
            first_token_ms=round(first * 1000),
            total_ms=round(total * 1000),
        )
        if error:
            devtools.log.warning("对话 · 请求失败：%s", error)
        else:
            body_tokens = devtools.estimate_tokens(text)
            reasoning_tokens = devtools.estimate_tokens(reasoning)
            notes = f"，其中思考 {reasoning_tokens}" if reasoning_tokens else ""
            if interrupted:
                notes += "，被打断"
            if silent:
                notes += "，未作回应"
            devtools.log.info(
                "对话 · 回复完成（用时 %.2fs，首字 %.2fs，约 %d token%s）",
                total,
                first,
                body_tokens + reasoning_tokens,
                notes,
            )
        self.busy_changed.emit(False)
        if error:
            self.failed.emit(error)
        elif silent:
            devtools.log.info("对话 · 未作回应（已记入会话日志，界面不显示）")
            self.silenced.emit()
        else:
            self.finished.emit(text)

    def _system_prompt(self) -> str:
        """拼成一条 system：每块前面都带一个大标题（见 app/conversation/prompts.py）。

        顺序是"通用约束 → 角色人设"：先定基调，再定人设。表情标记这类运行期说明
        交给 :meth:`_runtime_notes`，由 build_context 与当前时间说明合成同一节。
        """
        parts: list[str] = []
        base = str(self._base_prompt).strip()
        if base:
            parts.append(f"{prompts.BASIC}\n{base}")
        persona = str(self._info.get("system_prompt", "")).strip()
        if persona:
            parts.append(f"{prompts.PERSONA}\n{persona}")
        return "\n\n".join(parts)

    def _runtime_notes(self) -> list[str]:
        """运行期要额外告诉模型的说明，逐条传给 build_context 合并成【标记说明】。"""
        # 只在开启自动表情时才给，关掉就省下这段 token
        return [EXPRESSION_HINT] if self._auto_expression else []

    def _context_limit(self) -> int:
        try:
            return max(2, min(100, int(self._info.get("max_context_messages", DEFAULT_LIMIT))))
        except (TypeError, ValueError):
            return DEFAULT_LIMIT
