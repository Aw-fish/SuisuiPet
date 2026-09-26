"""记忆管理窗口：查看、编辑、删除长期记忆，手动触发整理。

记忆是用户的私人数据，也是模型可能会抽错的东西，所以这里把它完整摆在明面上：
每条记忆的类别、重要度、权重、被想起的次数与来源都看得到，可以随手纠正。
窗口独立于设置页，从「设置 → 角色设置 → 记忆」唤起。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app import characters
from app.conversation.memory import KINDS, KIND_LABELS, MemoryEntry, MemoryStore
from app.conversation.service import ConsolidationWorker
from app.ui.dialogs import ACCENT, StyledDialog, block_wheel

WINDOW_SIZE = (560, 660)


class MemoryEditDialog(QDialog):
    """编辑一条记忆的文字、类别与重要度。"""

    def __init__(self, parent: QWidget, entry: MemoryEntry) -> None:
        super().__init__(parent)
        self._drag: QPoint | None = None
        self._card: QFrame | None = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setMinimumWidth(440)
        card = QFrame(objectName="card")
        self._card = card
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(QLabel("编辑记忆", objectName="title"))
        self.text = QTextEdit(entry.text)
        self.text.setFixedHeight(88)
        layout.addWidget(self.text)
        row = QHBoxLayout()
        row.setSpacing(14)
        kind_box = QVBoxLayout()
        kind_box.setSpacing(4)
        kind_box.addWidget(QLabel("类别", objectName="miniLabel"))
        self.kind = QComboBox()
        for kind in KINDS:
            self.kind.addItem(KIND_LABELS[kind], kind)
        if entry.kind in KINDS:
            self.kind.setCurrentIndex(list(KINDS).index(entry.kind))
        kind_box.addWidget(self.kind)
        row.addLayout(kind_box, 1)
        weight_box = QVBoxLayout()
        weight_box.setSpacing(4)
        weight_box.addWidget(QLabel("重要度", objectName="miniLabel"))
        slider_row = QHBoxLayout()
        slider_row.setSpacing(8)
        self.importance = QSlider(Qt.Horizontal)
        self.importance.setRange(1, 5)
        self.importance.setValue(entry.importance)
        # 类别下拉与重要度滑块都不吃滚轮，免得滚页面时改到记忆
        self._wheel_guard = block_wheel(self, self.kind, self.importance)
        self.importance_value = QLabel(str(entry.importance), objectName="chip")
        self.importance_value.setFixedWidth(28)
        self.importance_value.setAlignment(Qt.AlignCenter)
        self.importance.valueChanged.connect(lambda value: self.importance_value.setText(str(value)))
        slider_row.addWidget(self.importance, 1)
        slider_row.addWidget(self.importance_value)
        weight_box.addLayout(slider_row)
        row.addLayout(weight_box, 1)
        layout.addLayout(row)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch()
        cancel = QPushButton("取消", objectName="ghost")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = QPushButton("保存", objectName="primary")
        ok.clicked.connect(self.accept)
        buttons.addWidget(ok)
        layout.addLayout(buttons)
        self.setStyleSheet(_QSS)
        card.installEventFilter(self)

    def eventFilter(self, watched, event):  # type: ignore[no-untyped-def]
        if watched is self._card:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            elif event.type() == QEvent.MouseMove and self._drag and event.buttons() & Qt.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag)
            elif event.type() == QEvent.MouseButtonRelease:
                self._drag = None
        return super().eventFilter(watched, event)

    def values(self) -> tuple[str, str, int]:
        return self.text.toPlainText().strip(), str(self.kind.currentData()), self.importance.value()


class MemoryWindow(QDialog):
    """角色的长期记忆列表：查看 / 编辑 / 删除 / 手动整理 / 全部重置。"""

    #: 记忆被清空后发出，交给主窗口把对话窗与当前会话也翻新
    reset_requested = Signal()

    def __init__(self, parent: QWidget | None, character: str) -> None:
        super().__init__(parent)
        self._character = ""
        self._store: MemoryStore | None = None
        self._worker: ConsolidationWorker | None = None
        self._drag: QPoint | None = None
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
        self.title = QLabel("记忆", objectName="title")
        bar_layout.addWidget(self.title)
        bar_layout.addStretch()
        close = QPushButton("×", objectName="close")
        close.clicked.connect(self.close)
        bar_layout.addWidget(close)
        layout.addWidget(bar)

        self.stats = QLabel("", objectName="hint")
        self.stats.setWordWrap(True)
        layout.addWidget(self.stats)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.filter = QComboBox()
        self.filter.addItem("全部类别", "")
        for kind in KINDS:
            self.filter.addItem(KIND_LABELS[kind], kind)
        self.filter.currentIndexChanged.connect(lambda _index: self.refresh())
        self._wheel_guard = block_wheel(self, self.filter)          # 筛选下拉不吃滚轮
        tools.addWidget(self.filter)
        tools.addStretch()
        self.consolidate_button = QPushButton("整理记忆", objectName="mini")
        self.consolidate_button.setToolTip("把还没整理的会话提炼成长期记忆")
        self.consolidate_button.clicked.connect(self._consolidate)
        tools.addWidget(self.consolidate_button)
        self.reset_button = QPushButton("重置", objectName="dangerMini")
        self.reset_button.setToolTip("删除全部记忆与对话记录，回到初始状态")
        self.reset_button.clicked.connect(self._reset)
        tools.addWidget(self.reset_button)
        layout.addLayout(tools)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch()
        self.scroll.setWidget(self.list_host)
        layout.addWidget(self.scroll, 1)

        self.setStyleSheet(_QSS)
        self.set_character(character)

    # ---- 数据 ---------------------------------------------------------------

    def set_character(self, name: str) -> None:
        if not name:
            return
        if name == self._character and self._store is not None:
            self.refresh()
            return
        self._character = name
        self._store = MemoryStore(name)
        self.title.setText(f"记忆 · {name}")
        self.refresh()

    def refresh(self) -> None:
        if self._store is None:
            return
        entries = self._store.entries()
        archived = self._store.archived()
        pending = self._store.pending()
        self.stats.setText(
            f"共 {len(entries)} 条长期记忆 · 归档 {len(archived)} 条 · 待整理会话 {len(pending)} 段"
            + ("（下次启动时自动整理）" if pending else "")
        )
        wanted = str(self.filter.currentData() or "")
        shown = [entry for entry in entries if not wanted or entry.kind == wanted]
        self._clear_layout(self.list_layout)
        if not shown:
            self.list_layout.addWidget(self._empty_hint("还没有长期记忆。聊过几轮之后，点「整理记忆」就会从这里长出来。"))
        for entry in shown:
            self.list_layout.addWidget(self._card(entry))
        if archived:
            self.list_layout.addWidget(QLabel("归档（权重衰减后移出检索池，不会影响对话）", objectName="section"))
            for entry in archived:
                self.list_layout.addWidget(self._card(entry, archived=True))
        self.list_layout.addStretch()

    @staticmethod
    def _empty_hint(text: str) -> QLabel:
        label = QLabel(text, objectName="hint")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignCenter)
        return label

    def _card(self, entry: MemoryEntry, archived: bool = False) -> QFrame:
        card = QFrame(objectName="memoryCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(QLabel(entry.label, objectName="kindChip"))
        head.addWidget(QLabel(f"重要度 {entry.importance}", objectName="meta"))
        head.addWidget(QLabel(f"权重 {entry.weight:.2f}", objectName="meta"))
        head.addWidget(QLabel(f"被想起 {entry.hits} 次", objectName="meta"))
        head.addStretch()
        if entry.ts:
            head.addWidget(QLabel(entry.ts[:10], objectName="meta"))
        layout.addLayout(head)
        text = QLabel(entry.text, objectName="memoryText")
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(text)
        foot = QHBoxLayout()
        foot.setSpacing(8)
        if entry.source:
            foot.addWidget(QLabel(f"来源 {entry.source}", objectName="meta"))
        foot.addStretch()
        if not archived:
            edit = QPushButton("编辑", objectName="mini")
            edit.clicked.connect(lambda checked=False, target=entry: self._edit(target))
            foot.addWidget(edit)
        delete = QPushButton("删除", objectName="dangerMini")
        delete.clicked.connect(lambda checked=False, target=entry, flag=archived: self._delete(target, flag))
        foot.addWidget(delete)
        layout.addLayout(foot)
        return card

    @staticmethod
    def _clear_layout(layout) -> None:  # type: ignore[no-untyped-def]
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            child = item.layout()
            if child is not None:
                MemoryWindow._clear_layout(child)

    # ---- 操作 ---------------------------------------------------------------

    def _edit(self, entry: MemoryEntry) -> None:
        dialog = MemoryEditDialog(self, entry)
        if dialog.exec() != QDialog.Accepted:
            return
        text, kind, importance = dialog.values()
        if not text or self._store is None:
            return
        self._store.update(entry.id, text, kind, importance)
        self.refresh()

    def _delete(self, entry: MemoryEntry, archived: bool) -> None:
        if not StyledDialog.confirm(
            self, "删除记忆", f"确定删除这条记忆吗？删除后无法恢复。\n\n{entry.text}", danger=True
        ):
            return
        if self._store is None:
            return
        if archived:
            self._store.forget_archived([entry.id])
        else:
            self._store.forget([entry.id])
        self.refresh()

    def _consolidate(self) -> None:
        if self._store is None or self._worker is not None:
            return
        info = characters.load_character(self._character)
        if not str(info.get("base_url", "")).strip() or not str(info.get("model", "")).strip():
            StyledDialog.notice(self, "整理记忆", "先在「设置 → 角色设置」里填好 API 地址与模型名称。")
            return
        self.consolidate_button.setEnabled(False)
        self.consolidate_button.setText("整理中…")
        worker = ConsolidationWorker(self._store, info, self)
        worker.done.connect(self._on_consolidated)
        worker.failed.connect(self._on_consolidation_failed)
        worker.finished.connect(self._on_worker_finished)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _on_worker_finished(self) -> None:
        self._worker = None
        self.consolidate_button.setEnabled(True)
        self.consolidate_button.setText("整理记忆")

    def _on_consolidated(self, added: int, merged: int) -> None:
        self.refresh()
        StyledDialog.notice(self, "整理记忆", f"整理完成：新增 {added} 条，合并 {merged} 条。")

    def _on_consolidation_failed(self, message: str) -> None:
        self.refresh()
        StyledDialog.notice(
            self, "整理记忆", f"整理失败：{message}\n\n待整理的会话会保留，之后再试即可。"
        )

    def _reset(self) -> None:
        if self._store is None:
            return
        if not StyledDialog.confirm(
            self,
            "重置记忆",
            f"将删除角色「{self._character}」的全部内容：\n\n"
            "· 长期记忆与归档\n· 月度摘要\n· 全部对话记录\n\n"
            "删除后无法恢复，别名表会保留。确定继续吗？",
            confirm="全部删除",
            danger=True,
        ):
            return
        removed = self._store.reset()
        self._store.new_session()
        self.refresh()
        # 通知主窗口把对话窗与当前会话也翻新
        self.reset_requested.emit()
        StyledDialog.notice(
            self, "重置记忆", f"已删除 {removed} 个文件，记忆与对话记录都已回到初始状态。"
        )

    # ---- 窗口 ---------------------------------------------------------------

    def eventFilter(self, watched, event):  # type: ignore[no-untyped-def]
        if watched is self._bar:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                self._bar.setCursor(Qt.ClosedHandCursor)
                return True
            if event.type() == QEvent.MouseMove and self._drag and event.buttons() & Qt.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag)
                return True
            if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                self._drag = None
                self._bar.setCursor(Qt.OpenHandCursor)
                return True
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.refresh()
        super().showEvent(event)


_QSS = (
    'QDialog { background: transparent; }'
    'QFrame#root { background:#FFFFFF; border:1px solid #ECEAF2; border-radius:16px; }'
    'QFrame#dragBar { background:transparent; }'
    'QFrame#memoryCard { background:#FBFAFE; border:1px solid #EFEDF5; border-radius:12px; }'
    'QLabel#title { color:#302C40; font:600 15px "Microsoft YaHei UI"; }'
    'QLabel#hint { color:#9993A5; font:10px "Microsoft YaHei UI"; }'
    'QLabel#meta { color:#A9A3B6; font:10px "Microsoft YaHei UI"; }'
    'QLabel#memoryText { color:#4C4759; font:11px "Microsoft YaHei UI"; }'
    'QLabel#section { color:#625D70; font:600 11px "Microsoft YaHei UI"; margin-top:8px; }'
    'QLabel#miniLabel { color:#625D70; font:600 11px "Microsoft YaHei UI"; }'
    'QLabel#kindChip { background:#F0EDFB; color:#6D5DC0; border-radius:8px; padding:3px 8px; font:600 10px "Microsoft YaHei UI"; }'
    'QLabel#chip { background:#F4F1FB; border:1px solid #E9E5F5; border-radius:8px; color:#6D5DC0; font:600 11px "Microsoft YaHei UI"; }'
    'QComboBox { background:white; border:1px solid #E9E7EF; border-radius:9px; padding:6px 10px; color:#403C4B; min-height:18px; }'
    'QComboBox:focus { border-color:#B8AAF3; }'
    'QTextEdit { background:white; border:1px solid #E9E7EF; border-radius:9px; padding:8px 10px; color:#403C4B; }'
    'QTextEdit:focus { border-color:#B8AAF3; }'
    f'QPushButton#primary {{ background:{ACCENT}; border:0; border-radius:9px; color:white; font-weight:600; padding:9px 20px; }}'
    'QPushButton#primary:hover { background:#806CD9; }'
    'QPushButton#ghost { background:#F4F1FB; border:1px solid #E9E5F5; border-radius:9px; color:#6D5DC0; font-weight:600; padding:9px 18px; }'
    'QPushButton#ghost:hover { background:#EBE6FA; }'
    'QPushButton#mini { background:#F4F1FB; border:1px solid #E9E5F5; border-radius:8px; color:#6D5DC0; font:600 11px "Microsoft YaHei UI"; padding:6px 10px; }'
    'QPushButton#mini:hover { background:#EBE6FA; }'
    'QPushButton#dangerMini { background:#FDF2F2; border:1px solid #F5DCDC; border-radius:8px; color:#C25554; font:600 11px "Microsoft YaHei UI"; padding:6px 10px; }'
    'QPushButton#dangerMini:hover { background:#FBE9E9; }'
    'QPushButton#close { border:0; background:transparent; color:#A39CAD; font-size:17px; min-width:24px; max-width:24px; }'
    'QPushButton#close:hover { color:#685D78; background:#F4F1FA; border-radius:8px; }'
    f'QSlider::groove:horizontal {{ height:6px; background:#EFEDF7; border-radius:3px; }}'
    f'QSlider::sub-page:horizontal {{ background:{ACCENT}; border-radius:3px; }}'
    'QSlider::add-page:horizontal { background:#EFEDF7; border-radius:3px; }'
    f'QSlider::handle:horizontal {{ width:12px; height:12px; margin:-5px 0; background:white; border:2px solid {ACCENT}; border-radius:8px; }}'
    'QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }'
    'QScrollBar:vertical { background:transparent; width:8px; margin:0; }'
    'QScrollBar::handle:vertical { background:#DFDAEC; border-radius:4px; min-height:28px; }'
    'QScrollBar::handle:vertical:hover { background:#CFC8E0; }'
    'QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }'
    'QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical { background:transparent; }'
)
