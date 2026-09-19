"""开发者面板：把「模型实际收到了什么」和「程序在做什么」摊开看。

上面是对话链路（每次请求拼出的系统段、对话背景、每一轮消息与时间，以及耗时），
下面是运行日志。数据来自 :mod:`app.devtools`，只在开关打开时才有记录，
且只存在内存里。

走的是「事件流」而不是「格式化后的对话记录」：面板要能回答的是"这一次到底发了
什么、用了多久、哪一步出错了"，按发生顺序铺开最直观，也不会在字段变动时失准。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app import devtools
from app.conversation.message import NARRATION_TAG

WINDOW_SIZE = (820, 640)
#: 一条日志最多占几行（超长的在面板里截断显示，避免刷屏）
MAX_LOG_LINES = 60

#: 与 app/conversation/message.py 的 ROLE_* 对齐，只用于显示
ROLE_LABELS = {"system": "system", "user": "user", "assistant": "assistant", "tool": "tool"}


def _clock(value: str) -> str:
    """把 ISO 时间压成 HH:MM:SS，面板里看着清爽。"""
    text = str(value)
    return text[11:19] if len(text) >= 19 else text


def _first_line(text: str, limit: int = 44) -> str:
    for line in str(text).splitlines():
        line = line.strip()
        if line:
            return line[:limit] + ("…" if len(line) > limit else "")
    return "（空）"


def _block(text: str) -> str:
    return "\n".join("    " + line for line in str(text).splitlines())


def format_event(event: dict) -> str:
    """把一条记录渲染成面板里的文本。纯函数，便于单独验证。"""
    kind = str(event.get("kind", ""))
    when = _clock(str(event.get("ts", "")))
    if kind == "log":
        return f"[{when}] {str(event.get('level', 'INFO')):<7} {event.get('text', '')}"
    if kind == "context":
        sections = list(event.get("sections") or [])
        turns = list(event.get("turns") or [])
        narration = str(event.get("narration") or "")
        lines = [
            f"═══ {when}  上下文 · 角色 {event.get('character') or '—'}"
            f" · {len(sections)} 个系统段 + {len(turns)} 轮消息"
        ]
        for index, section in enumerate(sections, 1):
            text = str(section)
            lines.append(f"  [system {index}] {len(text)} 字 · {_first_line(text)}")
            lines.append(_block(text))
        # 对话背景挂在最后一条**用户**消息上（与 build_context 的规则一致）
        latest_user = max(
            (index for index, turn in enumerate(turns) if turn.get("role") == "user"), default=-1
        )
        for index, turn in enumerate(turns):
            role = ROLE_LABELS.get(str(turn.get("role")), str(turn.get("role")))
            content = str(turn.get("content", ""))
            carries_narration = bool(narration) and index == latest_user
            notes: list[str] = []
            if carries_narration:
                notes.append("含对话背景")
            if turn.get("interrupted"):
                notes.append("被打断（发出时末尾加 [被打断]）")
            suffix = "  · " + " · ".join(notes) if notes else ""
            lines.append(f"  [{role}] {_clock(str(turn.get('ts', '')))} · {len(content)} 字{suffix}")
            if carries_narration:
                lines.append(_block(f"{NARRATION_TAG} {narration}"))
            if content.strip():
                lines.append(_block(content))
        return "\n".join(lines)
    if kind == "request":
        return (
            f"  → {when}  参数与用量 · {event.get('model')}"
            f" · temperature {event.get('temperature')}"
            f" · 上下文上限 {event.get('context_limit')} 条"
            f" · 实发 {event.get('count')} 条 / {event.get('chars')} 字"
        )
    if kind == "reply":
        text = str(event.get("text", ""))
        notes = []
        if event.get("interrupted"):
            notes.append("被打断")
        if event.get("error"):
            notes.append(f"错误：{event.get('error')}")
        line = (
            f"  ← {when}  回复 · 用时 {int(event.get('total_ms') or 0) / 1000:.2f}s"
            f" · 首字 {int(event.get('first_token_ms') or 0) / 1000:.2f}s · {len(text)} 字"
        )
        if notes:
            line += " · " + " · ".join(notes)
        if text.strip():
            line += "\n" + _block(text)
        return line
    if kind == "consolidated":
        return f"  ✓ {when}  记忆整理 · 新增 {event.get('added')} 条，合并 {event.get('merged')} 条"
    return f"[{when}] {kind} {event}"


class DevWindow(QDialog):
    """开发者面板窗口（从「设置 → 开发者」打开）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drag: QPoint | None = None
        self._bar: QFrame | None = None
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

        self.stats = QLabel(
            "记录只存在内存里，关掉开关就清空；窗口内容可以直接选中复制。", objectName="hint"
        )
        self.stats.setWordWrap(True)
        layout.addWidget(self.stats)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.follow = QCheckBox("自动滚到底部")
        self.follow.setChecked(True)
        controls.addWidget(self.follow)
        controls.addStretch()
        clear = QPushButton("清空", objectName="mini")
        clear.clicked.connect(self._clear_views)
        controls.addWidget(clear)
        layout.addLayout(controls)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._pane("对话链路（模型实际收到什么）", "conversation"))
        splitter.addWidget(self._pane("日志", "log"))
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.setStyleSheet(_QSS)
        devtools.subscribe(self._on_event)
        self._reload()

    def _pane(self, title: str, attr: str) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(QLabel(title, objectName="section"))
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.NoWrap)
        view.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(view, 1)
        setattr(self, attr, view)
        return host

    # ---- 数据 ---------------------------------------------------------------

    def _reload(self) -> None:
        self.conversation.clear()
        self.log.clear()
        for event in devtools.records():
            self._show(event)

    def _on_event(self, event: dict) -> None:
        if event.get("kind") == "clear":
            self._clear_views()
            return
        self._show(event)

    def _show(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "log":
            view = self.log
        elif kind == "clear":
            return
        else:
            view = self.conversation
        self._append(view, format_event(event))
        self._update_stats()

    def _append(self, view: QPlainTextEdit, text: str) -> None:
        lines = text.splitlines() or [""]
        if len(lines) > MAX_LOG_LINES:
            lines = lines[:MAX_LOG_LINES] + [f"    ……（本行共 {len(lines)} 行，已截断显示）"]
        view.appendPlainText("\n".join(lines))
        if self.follow.isChecked():
            bar = view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _clear_views(self) -> None:
        self.conversation.clear()
        self.log.clear()
        self._update_stats()

    def _update_stats(self) -> None:
        events = devtools.records()
        conversation = sum(1 for event in events if event.get("kind") not in ("log", "clear"))
        logs = sum(1 for event in events if event.get("kind") == "log")
        self.stats.setText(
            f"已记录：对话事件 {conversation} 条 · 日志 {logs} 条"
            "（只存在内存里，关掉开关即清空）"
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
QLabel#title { color:#302C40; font:600 15px "Microsoft YaHei UI"; }
QLabel#hint { color:#9993A5; font:10px "Microsoft YaHei UI"; }
QLabel#section { color:#625D70; font:600 11px "Microsoft YaHei UI"; }
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
