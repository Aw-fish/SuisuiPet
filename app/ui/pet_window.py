"""Interactive desktop pet window and its floating utilities."""
from __future__ import annotations
import random
from pathlib import Path
from typing import Callable
from PySide6.QtCore import QEvent, QPropertyAnimation, QPoint, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QContextMenuEvent, QFontMetrics, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget
from app import characters
from app.config import load_settings, save_settings
from app.conversation.message import ROLE_ASSISTANT, ROLE_USER
from app.conversation.service import ConversationService
from app.pet.animator import SpriteAnimator
from app.pet.character_sprite import CharacterAssets
from app.ui.dialogs import StyledDialog
from app.ui.menus import styled_menu

#: 立绘在桌面上的显示高度（像素）
SPRITE_HEIGHT = 224
#: 角色上方气泡提示占用的高度
BUBBLE_HEIGHT = 34
#: 找不到立绘时的窗口尺寸（窗口会被隐藏，仅用于几何计算）
DEFAULT_CANVAS_SIZE = QSize(168, 168)
#: 打开对话窗时最多恢复多少条历史
HISTORY_LIMIT = 30
#: 对话窗尺寸（竖长比例，贴近手机聊天界面）
CHAT_SIZE = QSize(400, 600)
#: 单个气泡最多占消息区宽度的比例
BUBBLE_MAX_RATIO = 0.9
#: 气泡左右内边距 + 边框，与 CHAT_QSS 里的 padding 保持一致
BUBBLE_PADDING = 26
#: 宽度测量余量。实测 QLabel 的换行判定比 QFontMetrics 的度量值多吃约 6px：
#: 按"度量值恰好相等"给宽度，短消息会被白白折成两行。这里留 8px 富余。
BUBBLE_SLACK = 8
#: 单条气泡的最小宽度
BUBBLE_MIN_WIDTH = 52

ACTIVITY_MIN, ACTIVITY_MAX, ACTIVITY_DEFAULT = 1, 10, 5

#: 立绘素材本身的朝向：True 表示默认朝向屏幕左侧。
#: 随机移动按这张表决定镜像；拖拽沿用"向左即镜像"的写法。
ART_FACES_LEFT = True


def wander_mirrored(delta_x: int) -> bool:
    """随机移动时的镜像规则：素材朝左时，向右移动才需要镜像。"""
    if delta_x == 0:
        return False
    return (delta_x > 0) == ART_FACES_LEFT


def drag_mirrored(delta_x: int) -> bool:
    """拖拽时的镜像规则：向左拖即镜像。"""
    return delta_x < 0


def current_character(data: dict) -> tuple[str, dict]:
    """返回 (角色名, 角色配置)；没有选中角色时返回空配置。"""
    name = str(data.get("character", {}).get("selected") or "").strip()
    if not name:
        return "", {}
    return name, characters.load_character(name)


def character_folders(data: dict) -> list[Path]:
    """当前角色的候选立绘文件夹：外部路径优先，其次是角色目录下的 sprites。"""
    name, info = current_character(data)
    if not name or info.get("format", "img") != "img":
        return []
    candidates: list[Path] = []
    configured = str(info.get("asset_path", "")).strip()
    if configured:
        candidates.append(Path(configured))
    candidates.append(characters.sprite_dir(name, info))
    folders: list[Path] = []
    for candidate in candidates:
        if candidate.is_dir() and candidate not in folders:
            folders.append(candidate)
    return folders


def character_activity(data: dict) -> int:
    """读取当前角色的活跃度（1-10），越界或非法值回落到默认。"""
    _, info = current_character(data)
    try:
        return max(ACTIVITY_MIN, min(ACTIVITY_MAX, int(info.get("activity", ACTIVITY_DEFAULT))))
    except (TypeError, ValueError):
        return ACTIVITY_DEFAULT


def motion_profile(level: int) -> tuple[int, float, int]:
    """把 1-10 的活跃度换算成 (随机动作间隔ms, 触发概率, 移动距离px)。"""
    level = max(ACTIVITY_MIN, min(ACTIVITY_MAX, int(level)))
    interval = 9000 - (level - 1) * 700
    chance = 0.03 + (level - 1) * 0.055
    distance = 30 + (level - 1) * 12
    return interval, chance, distance


class ChatInput(QTextEdit):
    """Enter 发送、Shift+Enter 换行。"""

    submitted = Signal()

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ChatDialog(QDialog):
    """流式对话窗：中途可停止、支持历史恢复，生成时联动桌宠的说话动作。"""

    def __init__(self, parent: QWidget, conversation: ConversationService) -> None:
        super().__init__(parent)
        self.conversation = conversation
        self.drag: QPoint | None = None
        self._stream_label: QLabel | None = None
        self._buffer = ""
        self._follow = True
        #: 标记"当前这次滚动是程序发起的"，避免误判成用户手动滚动
        self._auto_scrolling = False
        self._reset_timer = QTimer(self)
        self._reset_timer.setInterval(40)
        self._reset_timer.timeout.connect(self._flush_stream)

        self.setFixedSize(CHAT_SIZE)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        root = QFrame(objectName="chatRoot")
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.addWidget(root)
        layout = QVBoxLayout(root); layout.setContentsMargins(12, 8, 12, 12); layout.setSpacing(8)
        # 顶栏做成聊天软件的两行表头：角色名 + 状态，标题居中，关闭键在右
        self.drag_bar = QFrame(objectName="dragBar"); self.drag_bar.setFixedHeight(46); self.drag_bar.setCursor(Qt.OpenHandCursor); self.drag_bar.installEventFilter(self)
        bar = QHBoxLayout(self.drag_bar); bar.setContentsMargins(0, 0, 0, 0); bar.setSpacing(6)
        bar.addSpacing(26)
        bar.addStretch()
        head = QVBoxLayout(); head.setSpacing(0); head.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("对话", objectName="chatTitle"); self.title.setAlignment(Qt.AlignCenter)
        self.status = QLabel("", objectName="chatStatus"); self.status.setAlignment(Qt.AlignCenter)
        head.addWidget(self.title); head.addWidget(self.status)
        bar.addLayout(head)
        bar.addStretch()
        close = QPushButton("×", objectName="chatClose"); close.clicked.connect(self.hide); bar.addWidget(close)
        layout.addWidget(self.drag_bar)
        line = QFrame(objectName="chatLine"); line.setFixedHeight(1); layout.addWidget(line)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded); self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._on_range_changed)
        self.messages = QWidget(); self.history = QVBoxLayout(self.messages); self.history.setContentsMargins(2, 6, 2, 2); self.history.setSpacing(10); self.history.addStretch(); self.scroll.setWidget(self.messages); layout.addWidget(self.scroll, 1)
        row = QHBoxLayout(); row.setSpacing(8)
        self.input = ChatInput(objectName="chatInput"); self.input.setPlaceholderText("说点什么……（Enter 发送，Shift+Enter 换行）"); self.input.setFixedHeight(44)
        self.input.submitted.connect(self._on_button)
        self.send_button = QPushButton("发送", objectName="send"); self.send_button.clicked.connect(self._on_button)
        row.addWidget(self.input, 1); row.addWidget(self.send_button); layout.addLayout(row)
        self.setStyleSheet(CHAT_QSS)
        conversation.delta.connect(self._on_delta)
        conversation.finished.connect(self._on_finished)
        conversation.failed.connect(self._on_failed)
        conversation.busy_changed.connect(self._on_busy_changed)

    # ---- 消息渲染 -----------------------------------------------------------

    def _message_area_width(self) -> int:
        """消息区可用宽度：窗口宽 - 外边距 - 滚动条。"""
        margins = 12 * 2 + 2 * 2
        return max(160, self.width() - margins - 10)

    def _bubble_width(self, label: QLabel, text: str) -> int:
        """气泡宽度 = min(整条文本的单行宽度, 限宽)。

        这里用 ``horizontalAdvance`` 量"一行到底"需要多宽：放得下就绝不被折行，
        放不下才交给 QLabel 在限宽内换行。早先用 ``boundingRect`` + TextWordWrap
        量，它会在临界点上提前折行，导致本来一行能放下的消息也被切成两行。
        """
        limit = max(BUBBLE_MIN_WIDTH, int(self._message_area_width() * BUBBLE_MAX_RATIO))
        metrics = QFontMetrics(label.font())
        natural = metrics.horizontalAdvance(text or " ") + BUBBLE_PADDING + BUBBLE_SLACK
        return max(BUBBLE_MIN_WIDTH, min(limit, natural))

    def add(self, text: str, user: bool) -> QLabel:
        label = QLabel(text, objectName="userMsg" if user else "petMsg")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 固定宽度 + heightForWidth，保证自动换行后高度算得准
        policy = QSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum)
        policy.setHeightForWidth(True)
        label.setSizePolicy(policy)
        label.setFixedWidth(self._bubble_width(label, text))
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
        if user: row.addStretch(); row.addWidget(label)
        else: row.addWidget(label); row.addStretch()
        self.history.insertLayout(self.history.count() - 1, row)
        # 新增一条消息属于"必须置底"的场景（包括自己刚发出的那条）
        self._scroll_to_bottom(force=True)
        return label

    def set_title(self, name: str) -> None:
        """标题显示角色名，像聊天窗口里的对方昵称。"""
        self.title.setText(name.strip() or "对话")

    def clear(self) -> None:
        self._stream_label = None
        while self.history.count() > 1:
            item = self.history.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            child = item.layout()
            if child is not None:
                self._clear_layout(child)

    def _clear_layout(self, layout) -> None:  # type: ignore[no-untyped-def]
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            child = item.layout()
            if child is not None:
                self._clear_layout(child)

    def load_history(self, messages) -> None:  # type: ignore[no-untyped-def]
        """把最近的会话渲染出来，重启后仍能看到上下文。"""
        self.clear()
        for message in messages:
            if message.role == ROLE_USER:
                self.add(message.content, True)
            elif message.role == ROLE_ASSISTANT and message.content:
                self.add(message.content + ("（已打断）" if message.interrupted else ""), False)
        self._follow = True
        self._scroll_to_bottom()

    # ---- 滚动跟随 -----------------------------------------------------------

    def _on_scrolled(self, value: int) -> None:
        # 只把"用户自己拖动滚动条"当成意图；程序触发的滚动不能反过来关掉跟随
        if self._auto_scrolling:
            return
        bar = self.scroll.verticalScrollBar()
        self._follow = value >= bar.maximum() - 8

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        """内容变高（折行变多、追加文字）时仍然钉在底部。

        QLabel 重新排版是异步的：置底时滚动条最大值还是旧的，等布局完成内容
        已经长出去了，所以必须在范围变化时再补一次。
        """
        if self._follow:
            self._apply_bottom()

    def _apply_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        self._auto_scrolling = True
        bar.setValue(bar.maximum())
        self._auto_scrolling = False

    def _scroll_to_bottom(self, force: bool = False) -> None:
        """跟随最后一条消息；``force`` 用于"发了消息 / 来了新回复"必须置底的场景。"""
        if force:
            self._follow = True
        if not self._follow:
            return
        self._apply_bottom()
        # 布局要等这一轮事件处理完才更新，届时要再钉一次
        QTimer.singleShot(0, self._deferred_bottom)

    def _deferred_bottom(self) -> None:
        if self._follow:
            self._apply_bottom()

    # ---- 发送 / 打断 --------------------------------------------------------

    def _on_button(self) -> None:
        if self.conversation.busy:
            self.status.setText("正在停止…")
            self.conversation.interrupt()
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self.add(text, True)
        self.status.setText("正在输入…")
        self.conversation.send(text)

    def _on_busy_changed(self, busy: bool) -> None:
        self.send_button.setText("停止" if busy else "发送")
        self.send_button.setObjectName("stop" if busy else "send")
        self.send_button.style().unpolish(self.send_button)
        self.send_button.style().polish(self.send_button)
        if busy:
            self._buffer = ""
            self._stream_label = self.add("", False)
            self.status.setText("正在输入…")
            self._reset_timer.start()
        else:
            # 注意：这里不能清 _stream_label。服务是「先 busy_changed(False)、
            # 后 finished(全文)」，提前清掉会让收尾的全文无处可写，回复尾部丢失。
            self._reset_timer.stop()

    def _on_delta(self, text: str) -> None:
        self._buffer += text

    def _flush_stream(self) -> None:
        label = self._stream_label
        if label is None:
            return
        text = self._buffer.strip()
        if label.text() == text:
            return
        label.setText(text)
        label.setFixedWidth(self._bubble_width(label, text))
        self._scroll_to_bottom()

    def _on_finished(self, text: str) -> None:
        self._reset_timer.stop()
        label = self._stream_label
        self._stream_label = None
        if label is not None:
            # 流式刷新是 40ms 抽帧，最后一批增量可能还没画上去，
            # 这里用完整文本无条件收尾，保证回复一个字符都不少。
            final = text.strip() or self._buffer.strip()
            if final:
                self._buffer = final
                if label.text() != final:
                    label.setText(final)
                label.setFixedWidth(self._bubble_width(label, final))
            elif not label.text():
                label.setText("（没有内容）")
                label.setFixedWidth(self._bubble_width(label, label.text()))
        self.status.setText("")
        # 一条完整回复落地，等同于来了一条新消息，直接跳到底部
        self._scroll_to_bottom(force=True)

    def _on_failed(self, message: str) -> None:
        self._reset_timer.stop()
        label = self._stream_label
        self._stream_label = None
        text = f"出错：{message}"
        if label is not None and not label.text():
            label.setText(text)
            label.setFixedWidth(self._bubble_width(label, text))
        else:
            self.add(text, False)
        self.status.setText("")
        self._scroll_to_bottom(force=True)

    # ---- 窗口 ---------------------------------------------------------------

    def eventFilter(self, watched, event):  # type: ignore[no-untyped-def]
        if watched is self.drag_bar:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self.drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft(); self.drag_bar.setCursor(Qt.ClosedHandCursor); return True
            if event.type() == QEvent.MouseMove and self.drag and event.buttons() & Qt.LeftButton:
                self.move(event.globalPosition().toPoint() - self.drag); return True
            if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                self.drag = None; self.drag_bar.setCursor(Qt.OpenHandCursor); return True
        return super().eventFilter(watched, event)

    def show_near(self, pet: QWidget) -> None:
        """贴在桌宠左侧显示；窗口变高后可能顶出屏幕，这里统一收进可用区域。"""
        area = pet.screen().availableGeometry(); point = pet.frameGeometry().topLeft() - QPoint(self.width() + 24, 150)
        x = max(area.left() + 8, min(point.x(), area.right() - self.width() - 8))
        y = max(area.top() + 8, min(point.y(), area.bottom() - self.height() - 8))
        self.move(x, y); self.show(); self.raise_(); self.activateWindow()


CHAT_QSS = (
    'QDialog { background: transparent; }'
    'QFrame#chatRoot { background:#FFFFFF; border:1px solid #EAE6F2; border-radius:18px; }'
    'QFrame#dragBar { background:transparent; }'
    'QFrame#chatLine { background:#F1EEF8; }'
    'QLabel#chatTitle { color:#3F3953; font:600 13px "Microsoft YaHei UI"; }'
    'QLabel#chatStatus { color:#A9A3B6; font:10px "Microsoft YaHei UI"; }'
    'QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }'
    'QScrollBar:vertical { background:transparent; width:8px; margin:0; }'
    'QScrollBar::handle:vertical { background:#DFDAEC; border-radius:4px; min-height:28px; }'
    'QScrollBar::handle:vertical:hover { background:#CFC8E0; }'
    'QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }'
    'QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical { background:transparent; }'
    'QTextEdit#chatInput { border:1px solid #E8E5F0; background:rgba(255,255,255,220); border-radius:10px; padding:8px 10px; color:#403C4B; }'
    'QTextEdit#chatInput:focus { border-color:#B8AAF3; }'
    'QPushButton#send { background:#917DE8; color:white; border:0; border-radius:10px; padding:9px 15px; }'
    'QPushButton#send:hover { background:#806CD9; }'
    'QPushButton#stop { background:#F2EFF9; color:#73688F; border:0; border-radius:10px; padding:9px 15px; }'
    'QPushButton#stop:hover { background:#E8E2F5; }'
    'QPushButton#chatClose { border:0; background:transparent; color:#A39CAD; font-size:17px; min-width:24px; max-width:24px; }'
    'QPushButton#chatClose:hover { color:#685D78; background:#F4F1FA; border-radius:8px; }'
    'QLabel#petMsg { background:#F6F4FB; color:#4C4759; border-radius:14px; padding:10px 13px; }'
    'QLabel#userMsg { background:#ECE7FE; color:#51457B; border-radius:14px; padding:10px 13px; }'
)


class TimerWindow(QWidget):
    def __init__(self, pet: QWidget, stop: Callable[[], None]) -> None:
        super().__init__(); self.pet = pet; self.setFixedSize(150, 62); self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint); self.setAttribute(Qt.WA_TranslucentBackground)
        root = QFrame(objectName="timerRoot"); layout = QHBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.addWidget(root); inner = QHBoxLayout(root); inner.setContentsMargins(12, 8, 10, 8)
        self.label = QLabel("25:00", objectName="timerLabel"); button = QPushButton("停止", objectName="stop"); button.clicked.connect(stop); inner.addWidget(self.label, 1); inner.addWidget(button)
        self.setStyleSheet('QFrame#timerRoot { background:rgba(255,255,255,235); border:1px solid #E8E4F2; border-radius:14px; } QLabel#timerLabel { color:#5C5275; font-weight:600; } QPushButton#stop { background:#F2EFF9; color:#73688F; border:0; border-radius:8px; padding:6px 9px; }')

    def place(self) -> None:
        point = self.pet.frameGeometry().topLeft() - QPoint(0, self.height() + 8); self.move(point)


class PetCanvas(QWidget):
    """只负责绘制立绘帧；没有素材时保持完全透明。"""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.animator = SpriteAnimator(self)
        self.animator.frame_changed.connect(self.update)
        self._scaled: tuple[QPixmap, QSize, QPixmap] | None = None

    @property
    def has_sprite(self) -> bool:
        return self.animator.current() is not None

    def set_assets(self, assets: CharacterAssets | None) -> None:
        self._scaled = None
        self.animator.set_assets(assets)
        if assets is not None:
            self.animator.set_state("Idle", restart=True)

    def set_state(self, state: str, mirrored: bool = False) -> None:
        if self.has_sprite:
            self.animator.set_state(state, mirrored=mirrored)

    def show_temporary(self, state: str, duration_ms: int) -> None:
        if self.has_sprite:
            self.animator.show_temporary(state, duration_ms)

    def pin_expression(self, state: str) -> None:
        if self.has_sprite:
            self.animator.pin(state)

    def clear_expression(self) -> None:
        self.animator.unpin()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        pixmap = self.animator.current()
        if pixmap is not None:
            self._paint_sprite(pixmap)

    def _paint_sprite(self, pixmap: QPixmap) -> None:
        # 用 pixmap 对象本身 + 画布尺寸做缓存标识，避免依赖 cacheKey 可能的重用
        if self._scaled is None or self._scaled[0] is not pixmap or self._scaled[1] != self.size():
            scaled = pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._scaled = (pixmap, self.size(), scaled)
        scaled = self._scaled[2]
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap((self.width() - scaled.width()) // 2, self.height() - scaled.height(), scaled)
        p.end()


class PetWindow(QWidget):
    def __init__(self, open_settings: Callable[[], None], refresh_settings: Callable[[dict], None]) -> None:
        super().__init__(); self.open_settings = open_settings; self.refresh_settings = refresh_settings; self.drag: QPoint | None = None; self.remaining = 0; self.assets: CharacterAssets | None = None; self.sprite_size = DEFAULT_CANVAS_SIZE; self.activity = ACTIVITY_DEFAULT; self.pinned_expression: str | None = None; self._expression_menu: QMenu | None = None; self._art_warned: str | None = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint); self.setAttribute(Qt.WA_TranslucentBackground)
        self.canvas = PetCanvas(self); self.bubble = QLabel(self); self.bubble.setAlignment(Qt.AlignCenter); self.bubble.setStyleSheet('background:rgba(255,255,255,235); color:#645C73; border:1px solid #EEEAF6; border-radius:12px; font-size:11px;'); self.bubble.hide()
        self._sync_geometry()
        self.animation = QPropertyAnimation(self, b'pos', self); self.animation.setDuration(650); self.animation.finished.connect(self._on_wander_finished)
        self.conversation = ConversationService(self); self.conversation.busy_changed.connect(self._on_conversation_busy)
        self.conversation.consolidated.connect(self._on_consolidated)
        self.chat = ChatDialog(self, self.conversation); self.timer_window = TimerWindow(self, self.stop_pomodoro); self.tick = QTimer(self); self.tick.timeout.connect(self._tick); self.wander = QTimer(self); self.wander.setInterval(5500); self.wander.timeout.connect(self._wander); self.wander.start(); self.load_character(load_settings()); self._place()
    def _place(self) -> None:
        area = self.screen().availableGeometry(); self.move(area.right()-self.width()-24, area.bottom()-self.height()-20)
    def _sync_geometry(self) -> None:
        if self.assets is not None:
            width = max(120, round(SPRITE_HEIGHT * self.sprite_size.width() / self.sprite_size.height())); height = SPRITE_HEIGHT
        else:
            width, height = DEFAULT_CANVAS_SIZE.width(), DEFAULT_CANVAS_SIZE.height()
        self.setFixedSize(width, height + BUBBLE_HEIGHT); self.canvas.setGeometry(0, BUBBLE_HEIGHT, width, height); self.bubble.setGeometry(8, 0, width - 16, BUBBLE_HEIGHT)
        if self.isVisible(): self._clamp_to_screen()
    def _clamp_to_screen(self) -> None:
        area = self.screen().availableGeometry(); self.move(max(area.left(), min(self.x(), area.right() - self.width())), max(area.top(), min(self.y(), area.bottom() - self.height())))
    def load_character(self, data: dict) -> None:
        """按当前角色的 img 立绘文件夹重建素材与窗口尺寸。"""
        folders = character_folders(data); assets = None
        for folder in folders:
            assets = CharacterAssets.load(folder)
            if assets is not None: break
        self.assets = assets; self.sprite_size = assets.size if assets is not None else DEFAULT_CANVAS_SIZE
        self.activity = character_activity(data); self.pinned_expression = None
        self.wander.setInterval(motion_profile(self.activity)[0])
        if not self.wander.isActive(): self.wander.start()
        self.canvas.set_assets(assets); self._sync_geometry()
        name, info = current_character(data)
        self.conversation.configure(name, info, data.get("conversation", {}).get("base_prompt", ""))
        self.chat.set_title(name)
        self.chat.load_history(self.conversation.history(HISTORY_LIMIT))
    def _on_conversation_busy(self, busy: bool) -> None:
        if busy: self.canvas.set_state('Talk')
        else: self._restore_state()
    def begin_new_session(self) -> None:
        """启动时开一段全新会话：历史靠长期记忆承载，对话窗从空开始。"""
        self.conversation.start_new_session(); self.chat.load_history([])
    def _on_consolidated(self, added: int, merged: int) -> None:
        if added > 0: self.say(f'记忆已整理，新增 {added} 条')
    def show_pet(self) -> None:
        """入口调用：有立绘就显示窗口，没有则隐藏并提示一次。"""
        if self.assets is not None:
            self._art_warned = None; self.show(); return
        self.hide()
        name, info = current_character(load_settings())
        folder = characters.sprite_dir(name, info) if name else None
        fingerprint = f"{name}|{folder}"
        if self._art_warned == fingerprint: return
        self._art_warned = fingerprint
        QTimer.singleShot(0, self._warn_missing_art)
    def _warn_missing_art(self) -> None:
        name, info = current_character(load_settings())
        folder = characters.sprite_dir(name, info) if name else None
        StyledDialog.notice(
            self, "找不到立绘！",
            f"角色“{name or '未选择'}”没有可用的立绘。\n\n"
            f"请把 Default.png 等图片放进：\n{folder or 'data/characters/<角色名>/sprites'}\n\n"
            "也可以在「设置 → 角色设置」里检查立绘目录或切换角色。",
        )
    def _set_expression(self, name: str | None) -> None:
        """固定显示某个表情立绘；恢复默认时回到默认立绘并重新开启随机动作。"""
        if name is not None and (self.assets is None or not self.assets.has(name)):
            return
        self.pinned_expression = name
        if name is None:
            self.canvas.clear_expression(); self._restore_state(); self.wander.start(); self.say('已恢复默认形象')
        else:
            self.wander.stop(); self.canvas.pin_expression(name); self.say(f"已切换为{CharacterAssets.expression_label(name)}")
    def _build_expression_menu(self, menu: QMenu) -> None:
        # 必须显式构造并持有引用：addMenu(str) 返回的子菜单会被 PySide 回收，导致子菜单失效
        submenu = styled_menu(menu, '表情')
        menu.addMenu(submenu); self._expression_menu = submenu
        restore = submenu.addAction('恢复默认'); restore.setCheckable(True); restore.setChecked(self.pinned_expression is None); restore.triggered.connect(lambda: self._set_expression(None))
        submenu.addSeparator()
        expressions = self.assets.expressions if self.assets is not None else []
        if not expressions:
            empty = submenu.addAction('暂无表情素材'); empty.setEnabled(False); return
        for expression in expressions:
            action = submenu.addAction(CharacterAssets.expression_label(expression)); action.setCheckable(True); action.setChecked(self.pinned_expression == expression); action.triggered.connect(lambda checked=False, target=expression: self._set_expression(target))
    def _base_state(self) -> str:
        return 'Focus' if self.tick.isActive() else 'Idle'
    def _restore_state(self) -> None:
        self.canvas.set_state(self._base_state())
    def _on_wander_finished(self) -> None:
        self._restore_state()
    def _build_context_menu(self) -> QMenu:
        d=load_settings(); movable=d['motion']['mode']=='movable'; m=styled_menu(self)
        a=m.addAction('切换为固定模式' if movable else '切换为自由模式'); a.triggered.connect(self.toggle_mode); m.addSeparator()
        self._build_expression_menu(m); m.addSeparator()
        a=m.addAction('关闭对话框' if self.chat.isVisible() else '打开对话框'); a.triggered.connect(self.close_chat if self.chat.isVisible() else lambda: self.chat.show_near(self)); a=m.addAction('停止番茄钟' if self.tick.isActive() else f"开始 {d['tools']['pomodoro_minutes']} 分钟番茄钟"); a.triggered.connect(self.stop_pomodoro if self.tick.isActive() else self.start_pomodoro); m.addSeparator(); a=m.addAction('设置'); a.triggered.connect(self.open_settings)
        return m
    def contextMenuEvent(self, e: QContextMenuEvent) -> None:
        self._build_context_menu().exec(e.globalPos())
    def mousePressEvent(self,e:QMouseEvent)->None:
        if e.button()==Qt.LeftButton: self.drag=e.globalPosition().toPoint()-self.frameGeometry().topLeft(); self.canvas.set_state('Drag')
    def mouseMoveEvent(self,e:QMouseEvent)->None:
        if self.drag and e.buttons()&Qt.LeftButton:
            position=e.globalPosition().toPoint()-self.drag
            self.canvas.set_state('Drag', mirrored=drag_mirrored(position.x()-self.x())); self.move(position); self.timer_window.place()
    def mouseReleaseEvent(self,e:QMouseEvent)->None:
        if e.button()==Qt.LeftButton: self.drag=None; self._restore_state()
    def moveEvent(self,e)->None:  # type: ignore[no-untyped-def]
        if hasattr(self,'timer_window') and self.timer_window.isVisible(): self.timer_window.place()
    def toggle_mode(self)->None:
        d=load_settings(); d['motion']['mode']='stationary' if d['motion']['mode']=='movable' else 'movable'; save_settings(d); self.refresh_settings(d); self.say('固定位置' if d['motion']['mode']=='stationary' else '自由移动')
    def apply_settings(self, data:dict)->None:
        if data['conversation']['show_floating_dialog']: self.chat.show_near(self)
        else: self.close_chat()
        self.load_character(data); self.show_pet()
        if self.isVisible(): self.say('设置已保存')
    def close_chat(self)->None: self.chat.hide()
    def start_pomodoro(self)->None:
        self.remaining=load_settings()['tools']['pomodoro_minutes']*60; self.tick.start(1000); self._timer_text(); self.timer_window.place(); self.timer_window.show(); self.canvas.set_state('Focus'); self.say('开始专注')
    def stop_pomodoro(self)->None: self.tick.stop(); self.timer_window.hide(); self.canvas.set_state('Idle'); self.say('番茄钟已停止')
    def _timer_text(self)->None: self.timer_window.label.setText(f'{self.remaining//60:02d}:{self.remaining%60:02d}')
    def _tick(self)->None:
        self.remaining-=1; self._timer_text()
        if self.remaining<=0: self.tick.stop(); self.timer_window.hide(); self.canvas.set_state('Idle'); self.say('专注完成！',5000)
    def _wander(self)->None:
        if self.pinned_expression is not None or load_settings()['motion']['mode']!='movable' or self.drag: return
        _, chance, distance = motion_profile(self.activity)
        if random.random()>chance: return
        area=self.screen().availableGeometry(); target=QPoint(max(area.left(),min(area.right()-self.width(),self.x()+random.randint(-distance,distance))),max(area.top(),min(area.bottom()-self.height(),self.y()+random.randint(-distance//2,distance//2)))); self.canvas.set_state('Move', mirrored=wander_mirrored(target.x()-self.x())); self.animation.stop(); self.animation.setStartValue(self.pos()); self.animation.setEndValue(target); self.animation.start()
    def say(self,text:str,duration:int=2300)->None:
        self.bubble.setText(text); self.bubble.show(); QTimer.singleShot(duration,self.bubble.hide); self.canvas.show_temporary('Talk',duration)





