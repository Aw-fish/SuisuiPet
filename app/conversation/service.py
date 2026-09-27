"""对话编排：组装上下文 → 调用 provider → 流式回调 → 落盘记忆。

界面只跟 :class:`ConversationService` 打交道：

* 发消息用 :meth:`send`，随时可以用 :meth:`interrupt` 打断；
* 通过 ``delta`` / ``sentence`` / ``finished`` / ``failed`` / ``busy_changed`` 信号回推。

网络请求跑在 :class:`QThread` 里，主线程不会被阻塞。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Sequence

from PySide6.QtCore import QObject, QThread, Signal

from app import devtools
from app.conversation import prompts
from app.conversation.consolidation import consolidate_pending
from app.conversation.memory import MemoryStore
from app.conversation.message import (
    SILENT_MARK,
    Message,
    ROLE_ASSISTANT,
    ROLE_TOOL,
    ROLE_USER,
    is_silent,
    silent_pending,
)
from app.conversation.providers.base import LLMProvider, ProviderError, ToolCall
from app.conversation.providers.openai_compat import OpenAICompatProvider
from app.conversation.sentence import SentenceSplitter
from app.pet.emotion import EXPRESSION_HINT, TagFilter, mood_asset
from app.skills.base import ToolRegistry

DEFAULT_LIMIT = 20
DEFAULT_TEMPERATURE = 0.8
DEFAULT_TIMEOUT = 60
#: 各事件的冷却秒数，拦截可能会频繁触发的功能
EVENT_COOLDOWN = {"拖动": 20, "记事板": 10}

#: 配置类问题（没填 API、Key / 地址 / 模型名不对）界面显示
CONFIG_ERROR_TEXT = "该功能需要检查api配置！"
CONFIG_CHECK_TEXT = "请检查api配置！"

#: 一轮对话里最多允许几轮"模型要工具 → 本地执行 → 结果回填"。
MAX_TOOL_ROUNDS = 3


def tool_result_text(name: str, ok: bool, result: str) -> str:
    """把技能结果反馈"""
    return f"[工具调用结果] {name}：{'调用成功' if ok else '调用失败'}\n{result}"


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
    """测试连接"""
    if not str(info.get("base_url", "")).strip():
        return False, CONFIG_CHECK_TEXT
    if not str(info.get("model", "")).strip():
        return False, CONFIG_CHECK_TEXT
    if not str(info.get("api_key", "")).strip():
        return False, CONFIG_CHECK_TEXT
    provider = build_provider(info)
    if provider is None:
        return False, CONFIG_CHECK_TEXT
    try:
        model = provider.probe()  # type: ignore[attr-defined]
    except ProviderError as exc:
        return False, CONFIG_CHECK_TEXT if exc.config else str(exc)
    except Exception as exc:  # noqa: BLE001 - 网络层异常统一兜住
        return False, f"测试失败：{exc}"
    return True, f"连接成功，模型 {model} 可用"


#: 推理内容转发给开发者面板的频率
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
    #: 要执行某个技能了：带一行给用户看的字（如"查天气"）
    tool_started = Signal(str)

    def __init__(
        self,
        provider: LLMProvider,
        messages: Sequence[dict],
        tools: Sequence[dict] | None = None,
        registry: ToolRegistry | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider
        self._messages = list(messages)
        #: 发给模型的工具说明；模型要调用时按注册表执行
        self._tools = list(tools or ())
        self._registry = registry if registry is not None else ToolRegistry()
        self._cancel = threading.Event()
        #: 这一轮攒下的推理内容（``reasoning_content``），收尾时交给开发者面板。
        #: 推理片段动辄上千个，不逐个发信号，只在这里累加。
        self.reasoning = ""
        #: 失败原因是"配置没弄好"（Key / 地址 / 模型名）：界面该换成一句人话
        self.config_error = False
        #: 这一轮真正执行了几个工具。收尾时用它判断该不该回滚历史：只要执行过工具，
        #: 模型就确实看到并处理了用户的话，哪怕最后没能说出正文也不能撤。
        self.tool_runs = 0
        #: 已完成的工具轮次（助手调用 + 结果），收尾时成对落进会话日志
        self.tool_exchange: list[dict[str, Any]] = []

    def cancel(self) -> None:
        self._cancel.set()
        self._provider.abort()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        parts: list[str] = []
        error = ""
        rounds = 0
        try:
            while True:
                # 轮数用完就不再带工具：模型只能拿已经查到的信息作答，不会无限查下去
                text, calls = self._stream_once(with_tools=rounds < MAX_TOOL_ROUNDS)
                if text:
                    parts.append(text)
                if self._cancel.is_set() or not calls or rounds >= MAX_TOOL_ROUNDS:
                    break
                rounds += 1
                self._run_tools(calls, text)
        except ProviderError as exc:
            error = str(exc)
            self.config_error = bool(getattr(exc, "config", False))
        except Exception as exc:  # noqa: BLE001 - 线程边界统一兜住
            error = f"请求出错：{exc}"
        # 主动打断会把阻塞中的连接掐断，读取随之抛异常——那是预期行为，不是失败。
        # 这里一律按正常结束交回去，由上层以「被打断」收尾并保留已生成的部分。
        if self._cancel.is_set():
            self.finished.emit("".join(parts))
            return
        if error:
            self.failed.emit(error)
            return
        self.finished.emit("".join(parts))

    def _stream_once(self, with_tools: bool) -> tuple[str, list[ToolCall]]:
        """跑一次流式请求：转发增量与推理，返回这一轮的正文与模型想要的工具调用。

        「收尾前把尾巴发出去」也在这里：推理是攒着发的（见 :data:`REASONING_FLUSH_CHARS`），
        每跑完一次都得补一次，否则最后不满一个窗口的那几句会卡在缓冲里。
        """
        parts: list[str] = []
        calls: list[ToolCall] = []
        speaking = reasoning = False
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
            for chunk in self._provider.stream(
                self._messages, tools=self._tools if with_tools else None, cancel=self._cancel
            ):
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
                if chunk.tool_calls:
                    calls.extend(chunk.tool_calls)
        finally:
            flush_reasoning()
        return "".join(parts), calls

    def _run_tools(self, calls: list[ToolCall], text: str = "") -> None:
        """执行工具：把调用与结果都写回上下文"""
        api_calls = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments or {}, ensure_ascii=False),
                },
            }
            for call in calls
        ]
        # text 是模型边说要查、边发起调用的那句话（多数模型这里是空的）
        self._messages.append({"role": ROLE_ASSISTANT, "content": text, "tool_calls": api_calls})
        results: list[dict[str, Any]] = []
        for call in calls:
            if self._cancel.is_set():
                devtools.log.info("技能 · 被打断，剩下的工具不再执行")
                break
            self.tool_started.emit(self._registry.label_for(call.name))
            devtools.log.info("技能 · 调用 %s(%s)", call.name, call.arguments)
            # 面板先摆一块"执行中…"：联网那几秒不长，但看得见比看不见好
            devtools.preview("tool", name=call.name, arguments=call.arguments or {})
            started = time.monotonic()
            ok, result = self._registry.run(call.name, call.arguments or {})
            devtools.record(
                "tool",
                name=call.name,
                arguments=call.arguments or {},
                result=result,
                ok=ok,
                seconds=round(time.monotonic() - started, 2),
            )
            # 回填的是带抬头的反馈文本：模型据此明确知道这次调用成没成
            self._messages.append(
                {"role": ROLE_TOOL, "tool_call_id": call.id, "content": tool_result_text(call.name, ok, result)}
            )
            results.append({"id": call.id, "name": call.name, "result": result, "ok": ok})
            self.tool_runs += 1
        if len(results) == len(api_calls):
            self.tool_exchange.append({"calls": api_calls, "results": results, "text": text})


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
            self.failed.emit("无法整理记忆，请检查API地址或模型名称配置")
            return
        try:
            added, merged = consolidate_pending(self._store, provider)
        except ProviderError as exc:
            self.failed.emit(CONFIG_ERROR_TEXT if getattr(exc, "config", False) else f"整理记忆失败：{exc}")
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
    #: 模型要执行某个技能了：带一行给用户看的字（如"查天气"），界面拿去显示一句提示
    tool_started = Signal(str)

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
        #: 这一轮追加进历史的用户消息 / 事件消息。请求失败且一个字都没产出时用它撤回——
        #: 模型压根没看到这条消息，就不该留在历史里被当成"用户说过的话"。
        self._round_message: Message | None = None
        #: 本轮从回复里剥掉了多少个表情标记（只给开发者面板看）
        self._tag_count = 0
        self._auto_expression = False
        #: 当前可用的技能（按设置组装，见 :func:`app.skills.build_registry`）
        self._skills = ToolRegistry()
        #: 发消息前问一句"要不要带动作前缀"（见 :meth:`set_prefix_provider`）
        self._prefix_provider: Callable[[], str] | None = None
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

    def configure(self, character: str, info: dict, base_prompt: str = "", skills: ToolRegistry | None = None) -> None:
        """切换当前角色：换角色同时切换记忆目录。

        ``base_prompt`` 是与角色无关的通用说话约束（全局设置），发请求时拼在
        角色提示词前面。``skills`` 是当前可用的技能集合（按设置组装）：关掉的技能
        不在里面，它的说明连请求都不会发出去——不传就沿用上一次的。
        """
        self._info = dict(info or {})
        self._base_prompt = base_prompt or ""
        if skills is not None:
            self._skills = skills
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

    def set_prefix_provider(self, provider: Callable[[], str] | None) -> None:
        """挂一个"这条用户消息要不要带动作前缀"的供应器（由界面提供，如演奏被打断）。

        服务层不关心演奏这回事，只负责把取回来的那一行塞进消息最前面（见
        :attr:`Message.prefix`）：所以两条发送入口（聊天窗、漂浮输入框）都不必各自处理。
        """
        self._prefix_provider = provider

    def _take_prefix(self) -> str:
        """取这一条消息的动作前缀；供应器出问题也不能拖垮发送。"""
        if self._prefix_provider is None:
            return ""
        try:
            return str(self._prefix_provider() or "").strip()
        except Exception:  # noqa: BLE001 - 界面侧的供应器不该让消息发不出去
            devtools.log.warning("对话 · 取动作前缀失败", exc_info=True)
            return ""

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
        self._start(provider, self._memory.append_user(text, self._take_prefix()), query=text)

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
            self.failed.emit(CONFIG_ERROR_TEXT)
        return None

    def _start(self, provider: LLMProvider, user_message: Message, query: str) -> None:
        """把一轮请求真正发出去：重置状态 → 记录参数 → 组装上下文 → 起线程。"""
        assert self._memory is not None          # 两个调用方都先查过
        self._round_message = user_message
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
        worker = _StreamWorker(provider, messages, self._skills.definitions(), self._skills, self)
        worker.delta.connect(self._on_delta)
        worker.reasoning_changed.connect(self._on_reasoning_changed)
        worker.reasoning_delta.connect(self._on_reasoning_delta)
        worker.tool_started.connect(self.tool_started)
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
        #: 失败属于"配置没弄好"：界面只提示去检查配置，细节留在日志与面板里
        config_error = bool(worker is not None and worker.config_error)
        # 模型明确表示"不作回应"（事件轮常见）：界面什么都不显示，但这一轮照样留在
        # 历史与日志里——上下文不断档，翻日志也看得出它看到了什么、选择了什么
        silent = not error and not interrupted and is_silent(text)
        if silent:
            text = ""
        if self._memory is not None and worker is not None and worker.tool_exchange:
            # 工具轮次也记进会话日志：翻记录时看得见"它查了什么、拿到了什么"。
            # 但它们**不进后续上下文**（见 build_context 的过滤）：助手侧的 tool_calls
            # 必须与紧随其后的 tool 结果成对出现，单独留下一条就不合法了。
            for tool_round in worker.tool_exchange:
                self._memory.append(
                    Message(
                        role=ROLE_ASSISTANT,
                        content=str(tool_round.get("text") or ""),
                        tool_calls=list(tool_round["calls"]),
                    )
                )
                for item in tool_round["results"]:
                    # 存的是**同一份**带抬头的反馈文本：模型这一轮看到什么，之后每一轮
                    # 从历史里看到的就该是什么，不然它会觉得"我明明调用过，怎么没记录"
                    self._memory.append(
                        Message(
                            role=ROLE_TOOL,
                            content=tool_result_text(
                                str(item.get("name") or ""), bool(item.get("ok", True)), str(item["result"])
                            ),
                            tool_call_id=item["id"],
                        )
                    )
        if self._memory is not None and (text or interrupted or silent):
            self._memory.append(
                Message(
                    role=ROLE_ASSISTANT,
                    content=SILENT_MARK if silent else text,
                    interrupted=interrupted,
                )
            )
        if (
            error
            and not text
            and not interrupted
            # 执行过工具就说明模型已经看到并处理了这条消息（哪怕最后一句没说出来），不能撤
            and not (worker is not None and worker.tool_runs)
            and self._memory is not None
            and self._round_message is not None
        ):
            # 请求失败且一个字都没产出：模型压根没看到这条消息（Key 不对、连不上），
            # 那就撤回刚写进历史的那一条——否则以后整理记忆时，会把"没人回应的话"
            # 当成用户确实说过的事记下来。
            if self._memory.discard(self._round_message):
                devtools.log.info("对话 · 这一轮没送出去，已撤回刚写入的历史")
        self._round_message = None
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
            # 配置类问题给一句人话；其余照实报（"出错："前缀统一在这里加，界面原样显示）
            self.failed.emit(CONFIG_ERROR_TEXT if config_error else f"出错：{error}")
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
