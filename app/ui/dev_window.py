"""开发者面板：把模型实收实返的原文摊开看。

画面是**一条连续的流水**（不是一轮一个盒子）：一条时间分隔线，接着这一轮发出去的
消息，然后是模型返回的原文。灰色小字是"其他信息"（token 估算、耗时、模型参数），
和正文分开；模型的**深度思考**收在可折叠的区块里：推理期间实时长出来、展开着，
正文一开始自动收起，收尾后默认收起——点标题随时展开（见 :class:`_Collapse`）。

消息编号 ``[1] [2] …`` 是它在**这次请求的消息数组**里的位置，整段会话连着数，
不会每轮从头开始。

显示上做三件事：

* **重复的不重印**：只把这一轮新增的消息追加在后面。两次请求之间会滑动上下文窗口
  （旧的被挤出去），所以不能按位置硬对，而是按内容找"上一轮的结尾 == 这一轮的开头"
  的最大重叠（见 :func:`_overlap`）；system 每条请求都要重拼（记忆注入随时在变），
  拿它比对会让后面全部跟着重印，因此单独用一行说明它变了哪几块；
* **发出去的东西照实显示**：`[当前时间]`、`[被打断]` 这些既然进了请求，就原样在
  正文里，面板不再另做区分或标注；
* **按 token 计量**：用 :func:`app.devtools.estimate_tokens` 估算（不引入分词器），
  所以一律标成"约"，只适合比较哪一段更贵，不能拿来对账。

走的是「事件流」：面板要回答的是"这一轮发了什么、回来了什么、贵在哪一段"，
按发生顺序铺开最直观，也不会在字段变动时失准。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app import devtools
from app.conversation.message import NARRATION_TAG

WINDOW_SIZE = (880, 660)
#: 一段正文最多铺几行（超长的截断显示，避免一轮就把面板刷满）
MAX_BODY_LINES = 60
#: 日志面板里一条最多铺几行
MAX_LOG_LINES = 40
#: 换个"对表"时机（重新挂时间行）时，分隔线上给一句提示，跟 ex.txt 的写法一致
NARRATION_NOTE = "【距上次需要重新输入当前时间】"

#: 流水里的文字分四种身份，各用各的颜色——"其他信息"一眼能和正文分开
HEAD = "head"    # 分隔线：这一轮什么时候发出
META = "meta"    # 其他：token 估算、耗时、模型参数
LABEL = "label"  # 消息标签：[3] assistant
BODY = "body"    # 正文：原样照搬的消息与回复

#: 与 app/conversation/message.py 的 ROLE_* 对齐，只用于显示
ROLE_LABELS = {"system": "system", "user": "user", "assistant": "assistant", "tool": "tool"}
#: 参数里只要这几项变了，就要重新显示一行
PARAM_KEYS = ("model", "temperature", "context_limit", "character")


def _clock(value: str) -> str:
    """把 ISO 时间压成 HH:MM:SS，面板里看着清爽。"""
    text = str(value)
    return text[11:19] if len(text) >= 19 else text


def _tokens(text: str) -> str:
    """统一写成"约 N token"——这是估算值，不能当计费口径用。"""
    return f"约 {devtools.estimate_tokens(text)} token"


def _content(message: dict) -> str:
    return str(message.get("content", ""))


def _clip(text: str, limit: int = MAX_BODY_LINES) -> str:
    """超长的正文截断显示，注明原文有多少行。"""
    body = str(text).strip("\n")
    lines = body.splitlines()
    if len(lines) <= limit:
        return body
    return "\n".join(lines[:limit]) + f"\n……（本段共 {len(lines)} 行，已截断显示）"


def _comparable(message: dict) -> str:
    """比对用的正文：去掉挂在前面的 `[当前时间]` 旁白行。

    时间行是消息自带的属性（不会挪走），但万一碰上"同一条消息换了旁白"这种边界，
    忽略它比对能少一次无谓的重印。
    """
    lines = _content(message).splitlines()
    if lines and lines[0].lstrip().startswith(NARRATION_TAG):
        lines = lines[1:]
    return "\n".join(lines)


def _same_message(left: dict, right: dict) -> bool:
    return left.get("role") == right.get("role") and _comparable(left) == _comparable(right)


def _same_params(left: dict, right: dict) -> bool:
    return all(left.get(key) == right.get(key) for key in PARAM_KEYS)


def _overlap(before: list[dict], now: list[dict]) -> int:
    """上一次请求的**结尾**有多少条原样出现在这次请求的开头。

    上下文窗口会滑动（旧的被挤出去），所以不能按位置硬对；按内容从长到短找最大
    重叠，就能接着上次铺过的地方往下走。找不到重叠时返回 0，整段重新铺。
    """
    limit = min(len(before), len(now))
    for size in range(limit, 0, -1):
        if all(_same_message(left, right) for left, right in zip(before[-size:], now[:size])):
            return size
    return 0


def _heading_of(line: str) -> str:
    """行首的``【标题】``，没有就返回空串（标题后面常跟着一句用途说明，不算标题）。"""
    stripped = line.strip()
    if not (stripped.startswith("【") and "】" in stripped):
        return ""
    return stripped[: stripped.index("】") + 1]


def _system_blocks(content: str) -> list[tuple[str, str]]:
    """把一条 system 按【标题】切成块，返回 ``[(标题, 文本)]``。

    只用来回答"这次 system 变了哪几块"——标题由 ``app/conversation/prompts.py`` 决定。
    """
    blocks: list[tuple[str, str]] = []
    heading = ""
    current: list[str] = []
    for line in str(content).splitlines():
        stripped = line.strip()
        if stripped.startswith("【") and "】" in stripped:
            chunk = "\n".join(current).strip()
            if chunk:
                blocks.append((heading, chunk))
            heading = _heading_of(line)
            current = [line]
        else:
            current.append(line)
    chunk = "\n".join(current).strip()
    if chunk:
        blocks.append((heading, chunk))
    return blocks


def _changed_heads(old: str, new: str) -> list[str]:
    """两条 system 里内容不同的那几块标题（空标题记作"开头"）。"""
    old_blocks = dict(_system_blocks(old))
    new_blocks = _system_blocks(new)
    heads = [head or "开头" for head, text in new_blocks if old_blocks.get(head) != text]
    heads += [head for head in old_blocks if head and head not in dict(new_blocks)]
    return heads


def _request_blocks(event: dict, previous: dict | None) -> list[tuple[str, str]]:
    """这一轮的分隔线；参数与上一轮相同时不再重复显示，改了才更新一行。"""
    when = _clock(str(event.get("ts", "")))
    note = NARRATION_NOTE if event.get("narration") else ""
    blocks: list[tuple[str, str]] = [(HEAD, f"━━━ {when} " + "━" * 14 + note)]
    if previous is not None and _same_params(previous, event):
        return blocks
    bits = [f"模型 {event.get('model') or '—'}"]
    if event.get("temperature") is not None:
        bits.append(f"temperature {event.get('temperature')}")
    bits.append(f"上下文上限 {event.get('context_limit')} 条")
    if event.get("character"):
        bits.append(f"角色 {event.get('character')}")
    note = "（与上一轮不同）" if previous is not None else ""
    blocks.append((META, "· " + " · ".join(bits) + note))
    return blocks


def _input_blocks(event: dict, previous: dict | None) -> list[tuple[str, str]]:
    """这一轮真正发出去的消息：与上一轮重合的部分不铺，只把新增的追加在后面。"""
    messages = list(event.get("messages") or [])
    before = list((previous or {}).get("messages") or [])
    total = sum(devtools.estimate_tokens(_content(item)) for item in messages)
    systems = sum(1 for item in messages if item.get("role") == "system")
    has_system = bool(messages) and messages[0].get("role") == "system"
    start = 1 if has_system else 0
    old_start = 1 if before and before[0].get("role") == "system" else 0
    size = _overlap(before[old_start:], messages[start:]) if previous is not None else 0
    added = len(messages) - start - size
    bits = [f"输入 {len(messages)} 条"]
    if systems:
        bits.append(f"system {systems} 条")
    bits.append(f"约 {total} token")
    if previous is not None:
        bits.append(f"新增 {added} 条" if added else "与上一轮完全相同")
    blocks: list[tuple[str, str]] = [(META, "· " + " · ".join(bits))]
    if has_system:
        if previous is None or not old_start:
            # 第一次拿到 system：整条铺开
            blocks.append((LABEL, "[1] system"))
            blocks.append((BODY, _clip(_content(messages[0]))))
        else:
            # system 每条请求都会重拼（记忆注入在变），只说明变了哪几块，不重印
            heads = _changed_heads(_content(before[0]), _content(messages[0]))
            if heads:
                blocks.append((META, f"· system 变了：{'、'.join(heads)}"))
    for index in range(start + size, len(messages)):
        message = messages[index]
        role = ROLE_LABELS.get(str(message.get("role")), str(message.get("role")))
        blocks.append((LABEL, f"[{index + 1}] {role}"))
        blocks.append((BODY, _clip(_content(message))))
    if not messages:
        blocks.append((META, "· （没有任何消息）"))
    return blocks


def _output_blocks(event: dict) -> list[tuple[str, str]]:
    """模型返回的原文（情绪标记还没剥掉的那一份）。"""
    raw = str(event.get("raw") or "") or str(event.get("text") or "")
    bits: list[str] = []
    if event.get("error"):
        bits.append(f"错误：{event.get('error')}")
    else:
        bits.append(f"用时 {int(event.get('total_ms') or 0) / 1000:.2f}s")
        first = int(event.get("first_token_ms") or 0)
        if first:
            bits.append(f"首字 {first / 1000:.2f}s")
        bits.append(_tokens(raw))
        if event.get("interrupted"):
            bits.append("被打断")
    blocks: list[tuple[str, str]] = [(META, "· " + " · ".join(bits))]
    blocks.append((BODY, _clip(raw)) if raw.strip() else (META, "· （没有内容）"))
    return blocks


def format_event(
    event: dict,
    previous_request: dict | None = None,
    previous_context: dict | None = None,
) -> list[tuple[str, str]]:
    """把一条记录渲染成 ``[(样式, 文本)]``。纯函数，便于单独验证。

    两个 previous 分别用来判断"参数有没有改""哪些消息是新增的"。
    深度思考不在其中——它要画成可折叠的控件，见 :func:`reasoning_text`。
    """
    kind = str(event.get("kind", ""))
    when = _clock(str(event.get("ts", "")))
    if kind == "log":
        return [(BODY, f"[{when}] {str(event.get('level', 'INFO')):<7} {event.get('text', '')}")]
    if kind == "request":
        return _request_blocks(event, previous_request)
    if kind == "context":
        return _input_blocks(event, previous_context)
    if kind == "reply":
        return _output_blocks(event)
    if kind == "consolidated":
        return [
            (HEAD, f"━━━ {when} " + "━" * 14),
            (META, f"· 记忆整理 · 新增 {event.get('added')} 条 · 合并 {event.get('merged')} 条"),
        ]
    return [(META, f"[{when}] {kind} {event}")]


def reasoning_text(event: dict) -> str:
    """这一轮的深度思考（``reasoning_content``）。没有推理阶段的模型返回空串。"""
    return str(event.get("reasoning") or "").strip("\n")


class _Collapse(QWidget):
    """可折叠区块：标题一行，点一下展开内容。

    深度思考是**边想边长**的，所以正文要能增量追加（见 :meth:`append`），标题里的
    token 估算跟着刷新。默认收起；推理进行中由面板展开，正文一开始再收回去。
    """

    def __init__(self, title: str, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._open = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(3)
        self.toggle = QToolButton(objectName="fold")
        self.toggle.setCheckable(True)
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.toggled.connect(self._on_toggled)
        layout.addWidget(self.toggle, 0, Qt.AlignLeft)
        self.body = QLabel(objectName="quote")
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.PlainText)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setVisible(False)
        layout.addWidget(self.body)
        self.set_text(text)

    @property
    def opened(self) -> bool:
        return self._open

    @property
    def text(self) -> str:
        return self.body.text()

    def append(self, text: str) -> None:
        """流式追加一段（推理片段到达时用）。"""
        self.set_text(f"{self.body.text()}{text}")

    def set_text(self, text: str) -> None:
        self.body.setText(_clip(text, MAX_BODY_LINES * 4))
        self._refresh()

    def set_open(self, opened: bool) -> None:
        self.toggle.setChecked(bool(opened))

    def _refresh(self) -> None:
        count = devtools.estimate_tokens(self.body.text())
        head = f"{self._title} · 约 {count} token" if count else self._title
        self.toggle.setText(("▾ " if self._open else "▸ ") + head)

    def _on_toggled(self, opened: bool) -> None:
        self._open = bool(opened)
        self.body.setVisible(self._open)
        self._refresh()


class _Stream(QFrame):
    """整段流水：分隔线、消息、回复、可折叠的深度思考，依次往下排。"""

    def __init__(self) -> None:
        super().__init__(objectName="stream")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 8, 10, 10)
        self._layout.setSpacing(3)
        self.reasoning: _Collapse | None = None
        self._spaced = False

    def add(self, style: str, text: str) -> None:
        widget = QLabel(text, objectName=style)
        widget.setWordWrap(True)
        widget.setTextFormat(Qt.PlainText)
        widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._layout.addWidget(widget)

    def new_turn(self) -> None:
        """一轮开始：允许下一处再留一次白。"""
        self._spaced = False
        self.reasoning = None

    def spacer(self) -> None:
        """正文之间留一点空，让"回来了什么"和上面几段分开。一轮只加一次。"""
        if self._spaced:
            return
        self._layout.addSpacing(6)
        self._spaced = True

    def add_reasoning(self, text: str = "") -> _Collapse:
        fold = _Collapse("深度思考", text)
        self.reasoning = fold
        self._layout.addWidget(fold)
        return fold

    def clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.reasoning = None
        self._spaced = False


class DevWindow(QDialog):
    """开发者面板窗口（从「设置 → 开发者」打开）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drag: QPoint | None = None
        self._bar: QFrame | None = None
        #: 上一次的请求 / 输入记录：用来判断参数有没有改、哪些消息是新增的
        self._last_request: dict | None = None
        self._last_context: dict | None = None
        #: 这一轮正在长的深度思考块（实时预览用；收尾那条记录才是权威数据）
        self._live_fold: _Collapse | None = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(False)
        self.resize(*WINDOW_SIZE)

        root = QFrame(objectName="root")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 12, 18, 16)
        layout.setSpacing(10)

        bar = QFrame(objectName="dragBar")
        bar.setFixedHeight(30)
        bar.setCursor(Qt.OpenHandCursor)
        bar.installEventFilter(self)
        self._bar = bar
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.addWidget(QLabel("开发者面板", objectName="title"))
        bar_layout.addStretch()
        close = QPushButton("×", objectName="close")
        close.clicked.connect(self.close)
        bar_layout.addWidget(close)
        layout.addWidget(bar)

        self.stats = QLabel(objectName="hint")
        self.stats.setWordWrap(True)
        layout.addWidget(self.stats)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.follow = QCheckBox("自动滚到底部")
        self.follow.setChecked(True)
        controls.addWidget(self.follow)
        controls.addStretch()
        # 「清除缓存」= 清空画面 + 删掉本地日志文件（见 devtools.clear）
        clear = QPushButton("清除缓存", objectName="mini")
        clear.clicked.connect(devtools.clear)
        controls.addWidget(clear)
        layout.addLayout(controls)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._stream_pane())
        splitter.addWidget(self._log_pane())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.setStyleSheet(_QSS)
        devtools.subscribe(self._on_event)
        self._reload()

    # ---- 界面骨架 -----------------------------------------------------------

    def _stream_pane(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(QLabel("发送记录（模型实收 / 实返）", objectName="section"))
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(2, 2, 2, 2)
        self._stream = _Stream()
        inner_layout.addWidget(self._stream)
        inner_layout.addStretch(1)
        view = QScrollArea()
        view.setWidgetResizable(True)
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setWidget(inner)
        layout.addWidget(view, 1)
        self.conversation = view
        return host

    def _log_pane(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(QLabel("运行日志", objectName="section"))
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.NoWrap)
        view.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(view, 1)
        self.log = view
        return host

    # ---- 数据 ---------------------------------------------------------------

    def _reload(self) -> None:
        self._reset()
        # 日志另外落在本地文件里：先把它铺上（日志记录就不必再从内存回放一遍）
        self.log.setPlainText("\n".join(devtools.saved_lines()))
        for event in devtools.records():
            if event.get("kind") != "log":
                self._show(event)
        self._follow_bottom()

    def _on_event(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "clear":
            self._clear_views()
            return
        if event.get("preview"):
            self._on_preview(str(kind), event)
            return
        self._show(event)

    def _on_preview(self, kind: str, event: dict) -> None:
        """进行中的状态：只影响流水末尾，不进历史，也不参与回放。"""
        if kind == "reasoning":
            fold = self._live_reasoning()
            if fold is not None:
                fold.append(str(event.get("text") or ""))
                self._follow_bottom()
        elif kind == "reasoning_done" and self._live_fold is not None:
            # 正文开始了：把深度思考收回来（跟主流 AI 前端一个做法）
            self._live_fold.set_open(False)

    def _live_reasoning(self) -> _Collapse | None:
        """当前这轮的深度思考块：第一次用到时现建，并展开着让内容长出来。"""
        if self._live_fold is None:
            self._stream.spacer()
            self._live_fold = self._stream.add_reasoning()
            self._live_fold.set_open(True)
        return self._live_fold

    def _finish_reasoning(self, event: dict) -> None:
        """收尾：把权威的推理内容盖到折叠块上，并收起来（收尾后默认收起）。"""
        reasoning = reasoning_text(event)
        if not reasoning and self._live_fold is None:
            return
        if self._live_fold is None:
            # 没实时流过（重开面板回看，或模型一次吐完）：直接建成收起的样子
            self._stream.spacer()
            self._live_fold = self._stream.add_reasoning(reasoning)
            return
        if reasoning:
            self._live_fold.set_text(reasoning)
        self._live_fold.set_open(False)

    def _show(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "clear":
            return
        if kind == "log":
            self._append_log(str(format_event(event)[0][1]))
            return
        if kind == "request":
            self._stream.new_turn()
            self._live_fold = None
        if kind == "reply":
            self._stream.spacer()
            self._finish_reasoning(event)
        for style, text in format_event(event, self._last_request, self._last_context):
            self._stream.add(style, text)
        if kind == "request":
            self._last_request = event
        elif kind == "context":
            self._last_context = event
        self._update_stats()
        self._follow_bottom()

    def _follow_bottom(self) -> None:
        if self.follow.isChecked():
            # 等布局算完再滚，否则拿到的是旧的滚动范围
            QTimer.singleShot(0, self._scroll_bottom)

    def _append_log(self, text: str) -> None:
        lines = text.splitlines() or [""]
        if len(lines) > MAX_LOG_LINES:
            lines = lines[:MAX_LOG_LINES] + [f"    ……（本行共 {len(lines)} 行，已截断显示）"]
        self.log.appendPlainText("\n".join(lines))
        if self.follow.isChecked():
            bar = self.log.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _scroll_bottom(self) -> None:
        bar = self.conversation.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _reset(self) -> None:
        self._stream.clear()
        self.log.clear()
        self._last_request = None
        self._last_context = None
        self._live_fold = None
        self._update_stats()

    def _clear_views(self) -> None:
        self._reset()

    def _update_stats(self) -> None:
        events = devtools.records()
        conversation = sum(1 for event in events if event.get("kind") not in ("log", "clear"))
        logs = sum(1 for event in events if event.get("kind") == "log")
        self.stats.setText(
            "灰色小字是 token 估算、耗时这类其他信息；模型返回的深度思考收在可点开的区块里。"
            f"已记录：对话事件 {conversation} 条 · 日志 {logs} 条（日志另存本地文件，"
            "「清除缓存」会一并删掉；正文可以直接选中复制）"
        )

    # ---- 窗口 ---------------------------------------------------------------

    def eventFilter(self, watched, event):  # type: ignore[no-untyped-def]
        if watched is self._bar:
            # 注意用 QEvent 上的名字：Qt.MouseButtonPress 是 Qt5 的写法，Qt6 里没有，
            # 写错会抛异常并直接从 C++ 虚函数里逃出去（表现为进程直接崩掉）
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                self._bar.setCursor(Qt.ClosedHandCursor)
            elif event.type() == QEvent.MouseMove and self._drag and event.buttons() & Qt.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag)
            elif event.type() == QEvent.MouseButtonRelease:
                self._drag = None
                self._bar.setCursor(Qt.OpenHandCursor)
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        # 关掉只是隐藏：窗口持有订阅，重新打开时还能接着看
        event.ignore()
        self.hide()


_QSS = '''
QFrame#root { background:white; border:1px solid #ECEAF2; border-radius:18px; }
QFrame#dragBar { background:transparent; }
QFrame#stream { background:#FBFAFE; border:1px solid #E9E7EF; border-radius:10px; }
QLabel#head { color:#6D5DC0; font:600 11px Consolas,"Microsoft YaHei UI",monospace; }
QLabel#meta { color:#A8A2B4; font:italic 10px "Microsoft YaHei UI"; }
QLabel#label { color:#6D5DC0; font:600 11px Consolas,"Microsoft YaHei UI",monospace; }
QLabel#body { color:#312D3D; font:11px Consolas,"Microsoft YaHei UI",monospace; }
QLabel#quote {
    color:#6A6478; font:11px Consolas,"Microsoft YaHei UI",monospace;
    background:#F4F1FB; border-left:3px solid #D9D1F0; border-radius:4px; padding:6px 8px;
}
QToolButton#fold { border:0; background:transparent; color:#7C6FC4; font:600 10px "Microsoft YaHei UI"; padding:1px 0; }
QToolButton#fold:hover { color:#5B4BA8; }
QLabel#title { color:#302C40; font:600 15px "Microsoft YaHei UI"; }
QLabel#hint { color:#9993A5; font:10px "Microsoft YaHei UI"; }
QLabel#section { color:#625D70; font:600 11px "Microsoft YaHei UI"; }
QScrollArea, QScrollArea > QWidget > QWidget { background:transparent; border:0; }
QPlainTextEdit {
    background:#FBFAFE; border:1px solid #E9E7EF; border-radius:9px;
    color:#403C4B; font:11px Consolas,"Microsoft YaHei UI",monospace; padding:6px;
}
QPushButton#close { border:0; background:transparent; color:#8E88A0; font:14px "Microsoft YaHei UI"; padding:2px 8px; }
QPushButton#close:hover { color:#E0526B; }
QPushButton#mini { border:1px solid #E4E1F0; border-radius:8px; background:white; color:#6D5DC0; padding:5px 12px; }
QPushButton#mini:hover { background:#F4F2FE; }
QCheckBox { color:#625D70; font:11px "Microsoft YaHei UI"; spacing:6px; }
'''
