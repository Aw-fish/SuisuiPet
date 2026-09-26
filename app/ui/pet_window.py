"""Interactive desktop pet window and its floating utilities."""
from __future__ import annotations
import ctypes
import math
import random
import sys
import time
from pathlib import Path
from typing import Callable
from PySide6.QtCore import QEvent, QPropertyAnimation, QPoint, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QActionGroup, QContextMenuEvent, QCursor, QFontMetrics, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget
from app import characters, devtools
from app.config import load_settings, save_settings
from app.conversation.message import ROLE_ASSISTANT, ROLE_USER, is_silent
from app.conversation.service import ConversationService
from app.pet.animator import SpriteAnimator
from app.pet.character_sprite import CharacterAssets
from app.pet.emotion import DEFAULT_SENSITIVITY, mood_timing
from app.ui.dialogs import StyledDialog
from app.ui.menus import styled_menu
from app.ui.notes_window import NOTES_OPACITY_DEFAULT, NotesWindow

#: 立绘在桌面上的显示高度（像素）
SPRITE_HEIGHT = 224
#: 角色上方气泡提示占用的高度
BUBBLE_HEIGHT = 34
#: 找不到立绘时的窗口尺寸（窗口会被隐藏，仅用于几何计算）
DEFAULT_CANVAS_SIZE = QSize(168, 168)
#: 打开对话窗时最多恢复多少条历史
HISTORY_LIMIT = 30
#: 拖动超过这么多像素才算一次「拖动」事件：随手点一下、抖几像素不该去打扰模型
DRAG_EVENT_MIN_PIXELS = 48
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

#: 对话形式：``window`` 聊天窗口 / ``bubble`` 漂浮输入框 + 角色上方的对白气泡
FORM_WINDOW, FORM_BUBBLE = "window", "bubble"
#: 记事板每次打开的落点（设置 → 工具）：角色旁 / 屏幕右下角 / 屏幕中间
NOTES_POSITION_PET, NOTES_POSITION_CORNER, NOTES_POSITION_CENTER = "pet", "corner", "center"
#: 贴屏幕角落 / 中间时的边距
WINDOW_MARGIN = 24
#: 对白气泡：窗口宽度上下限，以及"文字量 → 窗口尺寸"换算用的内边距与留量
SPEECH_MAX_WIDTH = 300
SPEECH_MIN_WIDTH = 110
SPEECH_PADDING = 32          # 左右内边距合计 + 边框
SPEECH_VPADDING = 22         # 上下内边距合计 + 边框
SPEECH_SLACK = 8             # 与对话窗同样的道理：QLabel 折行判定比度量值多吃几像素
#: 对白气泡与角色之间的留白，以及番茄钟浮窗显示时与它之间的留白
SPEECH_MARGIN = 10
SPEECH_TIMER_GAP = 8
#: 漂浮输入框尺寸
INPUT_SIZE = QSize(280, 46)
#: 对白气泡按 40ms 抽帧刷新：服务是逐块推进增量的，每块都重排一次会抖
SPEECH_FLUSH_MS = 40

ACTIVITY_MIN, ACTIVITY_MAX, ACTIVITY_DEFAULT = 1, 10, 5

#: 立绘素材本身的朝向：True 表示默认朝向屏幕左侧。
#: 随机移动按这张表决定镜像；拖拽沿用"向左即镜像"的写法。
ART_FACES_LEFT = True

#: 移动的恒定速度（像素/秒），随机游走与跟随鼠标共用。动画时长由距离换算而来，
#: 所以走得远近只影响走多久，不影响走多快。
MOVE_SPEED = 160
#: 单次移动的时长上下限，避免极短距离一闪而过、极长距离磨磨蹭蹭
MOVE_MIN_MS, MOVE_MAX_MS = 260, 2600

#: 跟随鼠标：多久看一眼鼠标在哪（毫秒）
FOLLOW_TICK_MS = 120
#: 跟随鼠标：一步最多走这么久，走完重新看一眼鼠标在哪。
#: 时长按 MOVE_SPEED 折算成距离，所以速度仍是那个恒定值，只是会不时修正方向。
FOLLOW_STEP_MS = 900
#: 跟随鼠标：停下来时离鼠标的余量（像素）。实际距离取窗口半对角线 + 这个余量，
#: 这样不管从哪个方向靠近，停下时鼠标一定在窗口之外——不压鼠标，也就不挡点击。
FOLLOW_MARGIN = 36

#: 气泡与"思考"动作的停留时长。切换模型 / 模式 / 表情 / 番茄钟这类状态提示都用它——
#: 它们只是"我知道了"，是原来时长的一半，不必一直挡在那；要看清的（任务完成）在调用处
#: 显式给更长的值。
SAY_MS = 1150

#: 重新声明「我是置顶窗口」的间隔（毫秒）。别的程序抢 z 序、或全屏程序来回切换之后，
#: 置顶属性可能被顶掉，桌宠就沉到别人窗口后面去了——"操作一阵子就被盖住"就是这么来的。
TOPMOST_CHECK_MS = 1500

#: 给 SetWindowPos 用的常量：放进"置顶"那一层，且不改尺寸、不动位置、不抢焦点
_HWND_TOPMOST = -1
_SWP_KEEP = 0x0001 | 0x0002 | 0x0010

#: 身上带着情绪表情时，随机移动的概率乘以这个系数。
#: 表情只在待机时露脸，一走起来就被 Move 素材盖住了，所以有表情时让它多待一会儿
#: —— 只是调低概率，不是禁止移动。
MOOD_MOVE_DAMPING = 0.3


def wander_mirrored(delta_x: int) -> bool:
    """随机移动时的镜像规则：素材朝左时，向右移动才需要镜像。"""
    if delta_x == 0:
        return False
    return (delta_x > 0) == ART_FACES_LEFT


def drag_mirrored(delta_x: int) -> bool:
    """拖拽时的镜像规则：向左拖即镜像。"""
    return delta_x < 0


#: 拖拽时方向翻转所需的最小累计位移（像素）。手抖会让相邻两次鼠标事件的位移方向
#: 来回变化，直接按单次位移判定，立绘就会左右抽动；这里累计到位再翻转。
DRAG_FLIP_THRESHOLD = 6


class DragDirection:
    """拖拽朝向判定器：首次移动直接定朝向，之后要累计越过阈值才翻转。"""

    def __init__(self, threshold: int = DRAG_FLIP_THRESHOLD) -> None:
        self._threshold = threshold
        self._drift = 0
        self._mirrored = False
        self._decided = False

    def reset(self) -> None:
        """每次按下滑鼠重新开始判定。"""
        self._drift = 0
        self._mirrored = False
        self._decided = False

    def update(self, delta_x: int) -> bool:
        if not self._decided:
            # 第一次真的动了，立刻定下朝向，不必等累积到阈值
            if delta_x == 0:
                return self._mirrored
            self._mirrored = drag_mirrored(delta_x)
            self._decided = True
            return self._mirrored
        self._drift += delta_x
        if abs(self._drift) >= self._threshold:
            self._mirrored = drag_mirrored(self._drift)
            self._drift = 0
        return self._mirrored


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


def move_duration(start: QPoint, target: QPoint) -> int:
    """按恒定速度把位移换算成动画时长。

    原来所有移动都固定 650ms，于是距离越远跑得越快；这里让时长正比于距离，
    移动速度就与距离无关了。
    """
    length = math.hypot(target.x() - start.x(), target.y() - start.y())
    return max(MOVE_MIN_MS, min(MOVE_MAX_MS, round(length / MOVE_SPEED * 1000)))


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
    """流式对话窗：中途可停止、支持历史恢复，生成时联动桌宠的动作。"""

    def __init__(self, parent: QWidget | None, conversation: ConversationService) -> None:
        # 刻意不认桌宠当 parent：Qt 里"子窗口永远浮在父窗口之上"，认了之后对话窗
        # 就会一直压住桌宠。传 None，把最上层留给桌宠。
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
        conversation.silenced.connect(self._on_silenced)

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
            # 事件行与"不作回应"都不进聊天界面：它们只留在请求、会话日志与开发者面板里，
            # 重新打开聊天窗口时也不该冒出来
            if message.role == ROLE_USER and not message.event:
                self.add(message.content, True)
            elif message.role == ROLE_ASSISTANT and message.content and not is_silent(message.content):
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
            # 消息等真有字再落（见 _flush_stream）：这样"不作回应"的一轮不会留下空壳
            self._stream_label = None
            self.status.setText("正在输入…")
            self._reset_timer.start()
        else:
            # 注意：这里不能清 _stream_label。服务是「先 busy_changed(False)、
            # 后 finished(全文)」，提前清掉会让收尾的全文无处可写，回复尾部丢失。
            self._reset_timer.stop()

    def _on_delta(self, text: str) -> None:
        self._buffer += text

    def _flush_stream(self) -> None:
        text = self._buffer.strip()
        if not text:
            return
        label = self._stream_label
        if label is None:
            # 第一段正文到了才落这条消息：没有字的一轮（比如"不作回应"）就不留空壳
            label = self._stream_label = self.add("", False)
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
                # 一个字都没生成（比如刚发出就被打断）：留个省略号，比"（没有内容）"自然
                label.setText("...")
                label.setFixedWidth(self._bubble_width(label, label.text()))
        self.status.setText("")
        # 一条完整回复落地，等同于来了一条新消息，直接跳到底部
        self._scroll_to_bottom(force=True)

    def _on_silenced(self) -> None:
        """模型对事件选择"不作回应"：界面不留任何痕迹（连空消息壳也不落）。"""
        self._reset_timer.stop()
        self._stream_label = None
        self.status.setText("")

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

    def show_at(self, point: QPoint) -> None:
        """在指定位置显示。落点由 PetWindow 按「设置 → 工具 → 对话窗口默认位置」算好。"""
        self.move(point); self.show(); self.raise_(); self.activateWindow()


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


#: 对白气泡背景的不透明度（百分比）取值范围与默认值。设置项是 ``conversation.bubble_opacity``，
#: 两端都要夹一下：低于 30% 基本看不清字了。
SPEECH_OPACITY_MIN, SPEECH_OPACITY_MAX, SPEECH_OPACITY_DEFAULT = 30, 100, 80


def speech_qss(opacity: int = SPEECH_OPACITY_DEFAULT) -> str:
    """对白气泡的样式：背景半透明，不透明度按设置来（百分比 → 0~255）。"""
    alpha = max(0, min(255, round(255 * opacity / 100)))
    return (
        f'QFrame#speechRoot {{ background:rgba(255,255,255,{alpha}); border:1px solid rgba(232,228,242,{alpha}); border-radius:14px; }}'
        'QLabel#speechText { color:#4B4560; font-size:12px; background:transparent; }'
    )

INPUT_QSS = (
    'QFrame#inputRoot { background:rgba(255,255,255,244); border:1px solid #E8E4F2; border-radius:14px; }'
    'QTextEdit#floatInput { background:transparent; border:0; color:#4B4560; font-size:12px; }'
    'QPushButton#floatSend { background:#EFEAF9; color:#6D5DC0; border:0; border-radius:9px; padding:6px 10px; }'
    'QPushButton#floatSend:hover { background:#E5DDF7; }'
    'QPushButton#floatClose { background:transparent; color:#B4AEC4; border:0; font-size:14px; }'
    'QPushButton#floatClose:hover { color:#6D5DC0; }'
)


class SpeechBubble(QWidget):
    """角色上方的对白框：随文字多少变高变宽，几秒后消失或被新回答覆盖。

    做成独立小窗口（认桌宠当 parent）而不是塞进桌宠窗口：桌宠窗口只有"立绘 + 顶部
    一条固定高度的气泡带"，放不下长短不一的对白。子窗口既不会被父窗口裁剪，也天然
    和角色同属一层、一起显隐；点一下它就能立刻收掉。
    """

    def __init__(self, pet: QWidget) -> None:
        super().__init__(pet)
        self.pet = pet
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        root = QFrame(objectName="speechRoot")
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.addWidget(root)
        inner = QVBoxLayout(root); inner.setContentsMargins(15, 10, 15, 10)
        self.label = QLabel("", objectName="speechText"); self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 固定宽度 + heightForWidth，折行之后高度才算得准（与对话窗里的气泡同一套做法）
        policy = QSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum); policy.setHeightForWidth(True)
        self.label.setSizePolicy(policy)
        inner.addWidget(self.label)
        self.set_opacity(SPEECH_OPACITY_DEFAULT)
        self._timer = QTimer(self); self._timer.setSingleShot(True); self._timer.timeout.connect(self.hide)

    def set_opacity(self, percent: int) -> None:
        """按设置调整背景不透明度（百分比，两端夹一下）。"""
        self.setStyleSheet(speech_qss(max(SPEECH_OPACITY_MIN, min(SPEECH_OPACITY_MAX, int(percent)))))

    def _label_width(self, text: str) -> int:
        """标签宽度：按"整条文本排成一行"来量，封顶上限；放不下就交给 QLabel 折行。"""
        metrics = QFontMetrics(self.label.font())
        natural = metrics.horizontalAdvance(text or " ") + SPEECH_SLACK
        low = SPEECH_MIN_WIDTH - SPEECH_PADDING
        high = SPEECH_MAX_WIDTH - SPEECH_PADDING
        return max(low, min(high, natural))

    def show_text(self, text: str, hold_ms: int = 0) -> None:
        """显示一段对白；``hold_ms`` > 0 时到点自动消失，0 表示先留着（流式期间）。"""
        text = text.strip()
        if not text:
            return
        width = self._label_width(text)
        self.label.setText(text)
        self.label.setFixedWidth(width)
        # 自己按折行结果算窗口大小（不依赖布局的 sizeHint）：文字多少直接决定窗口多大
        self.setFixedSize(width + SPEECH_PADDING, self.label.heightForWidth(width) + SPEECH_VPADDING)
        self.place()
        self.show(); self.raise_()
        self._timer.stop()
        if hold_ms > 0:
            self._timer.start(hold_ms)

    def place(self) -> None:
        """贴在角色头顶上方；顶出屏幕（或上方放不下）就向下收进可见区域。

        基准取"立绘的顶"（``pet.y() + BUBBLE_HEIGHT``）而不是窗口顶：窗口顶上还有一条
        提示带，按窗口顶算会让对白离角色脑袋远一截。番茄钟浮窗也在这一带，它让位给对白
        （见 :meth:`TimerWindow.place`），所以对白长高 / 收起来之后要叫它重新摆一次。
        """
        area = self.pet.screen().availableGeometry()
        x = self.pet.x() + (self.pet.width() - self.width()) // 2
        y = self.pet.y() + BUBBLE_HEIGHT - self.height() - SPEECH_MARGIN
        x = max(area.left() + 8, min(x, area.right() - self.width() - 8))
        y = max(area.top() + 8, min(y, area.bottom() - self.height() - 8))
        self.move(x, y)
        timer = getattr(self.pet, 'timer_window', None)
        if timer is not None and timer.isVisible(): timer.place()

    def hideEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        # 对白收起来了，让位抬高的番茄钟要放回原位
        timer = getattr(self.pet, 'timer_window', None)
        if timer is not None and timer.isVisible(): timer.place()
        super().hideEvent(event)

    def dismiss(self) -> None:
        self._timer.stop(); self.hide()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # 点一下就走：对白本来就是"看过就算"的东西
        self.dismiss()


class FloatingInput(QWidget):
    """贴在角色下方的漂浮输入框：Enter 发送、Esc 或 ✕ 收起。

    与对白框分工：对白在角色上方、输入在角色下方，两者不打架。
    """

    submitted = Signal(str)

    def __init__(self, pet: QWidget) -> None:
        super().__init__(pet)
        self.pet = pet
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(INPUT_SIZE)
        root = QFrame(objectName="inputRoot")
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.addWidget(root)
        row = QHBoxLayout(root); row.setContentsMargins(10, 6, 8, 6); row.setSpacing(6)
        self.input = ChatInput(objectName="floatInput")
        self.input.setPlaceholderText("说点什么……（Enter 发送，Esc 收起）")
        self.input.submitted.connect(self._on_submit)
        self.input.installEventFilter(self)          # Esc 收起（QTextEdit 不处理 Esc，得自己接）
        self.button = QPushButton("发送", objectName="floatSend")
        self.button.clicked.connect(self._on_submit)
        close = QPushButton("×", objectName="floatClose"); close.setToolTip("收起输入框")
        close.setFixedWidth(20); close.clicked.connect(self.hide)
        row.addWidget(self.input, 1); row.addWidget(self.button); row.addWidget(close)
        self.setStyleSheet(INPUT_QSS)

    def _on_submit(self) -> None:
        """发送；正在生成中时同一个按钮变成"停止"（与对话窗的按钮一致）。"""
        if self.pet.conversation.busy:
            self.pet.conversation.interrupt(); return
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self.submitted.emit(text)

    def set_busy(self, busy: bool) -> None:
        self.button.setText("停止" if busy else "发送")

    def open(self) -> None:
        self.place(); self.show(); self.raise_(); self.input.setFocus()

    def place(self) -> None:
        """贴在角色下方；下方放不下就改放上方，别压在角色身上（输入框不跟那些落点设置走）。"""
        area = self.pet.screen().availableGeometry()
        below = self.pet.y() + self.pet.height() + SPEECH_MARGIN
        above = self.pet.y() - self.height() - SPEECH_MARGIN
        y = below if below + self.height() <= area.bottom() - 8 else above
        x = self.pet.x() + (self.pet.width() - self.width()) // 2
        x = max(area.left() + 8, min(x, area.right() - self.width() - 8))
        y = max(area.top() + 8, min(y, area.bottom() - self.height() - 8))
        self.move(x, y)

    def eventFilter(self, watched, event):  # type: ignore[no-untyped-def]
        if watched is self.input and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.hide(); return True
        return super().eventFilter(watched, event)


def _reassert_topmost(window: QWidget) -> None:
    """重新声明某个窗口是「置顶」。

    ``raise_()`` 只能在当前层里往前挤，救不回已经被顶掉的置顶属性；Windows 上直接
    ``SetWindowPos(HWND_TOPMOST)`` 既重写属性，又不动尺寸位置、也不抢焦点
    （``SWP_NOACTIVATE``），是这件事最直接的做法。非 Windows 退回 ``raise_()``。
    """
    if sys.platform == 'win32':
        try:
            ctypes.windll.user32.SetWindowPos(int(window.winId()), _HWND_TOPMOST, 0, 0, 0, 0, _SWP_KEEP)
            return
        except Exception:  # pragma: no cover - 平台异常时退回 Qt 自己的手段
            devtools.log.warning('重新置顶失败，退回 raise()', exc_info=True)
    window.raise_()


class TimerWindow(QWidget):
    def __init__(self, pet: QWidget, pause: Callable[[], None], reset: Callable[[], None], stop: Callable[[], None]) -> None:
        # 认桌宠当 parent（+ 自己也置顶），是为了让浮窗和角色**同属一层**：Qt 与 Windows
        # 都保证子窗口在父窗口之上，所以相对关系永远稳定——不会时而人物压住浮窗、时而
        # 浮窗压住人物；顺带还能跟着桌宠一起显示与隐藏。
        super().__init__(pet); self.pet = pet; self.setFixedSize(212, 62); self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint); self.setAttribute(Qt.WA_TranslucentBackground)
        root = QFrame(objectName="timerRoot"); layout = QHBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.addWidget(root); inner = QHBoxLayout(root); inner.setContentsMargins(12, 8, 10, 8); inner.setSpacing(6)
        self.label = QLabel("25:00", objectName="timerLabel")
        self.pause = QPushButton("暂停", objectName="timerBtn"); self.pause.clicked.connect(pause)
        reset_button = QPushButton("重置", objectName="timerBtn"); reset_button.clicked.connect(reset)
        stop_button = QPushButton("停止", objectName="timerBtn"); stop_button.clicked.connect(stop)
        inner.addWidget(self.label, 1)
        for button in (self.pause, reset_button, stop_button): inner.addWidget(button)
        self.setStyleSheet('QFrame#timerRoot { background:rgba(255,255,255,235); border:1px solid #E8E4F2; border-radius:14px; } QLabel#timerLabel { color:#5C5275; font-weight:600; } QPushButton#timerBtn { background:#F2EFF9; color:#73688F; border:0; border-radius:8px; padding:6px 8px; } QPushButton#timerBtn:hover { background:#E9E3F7; }')

    def place(self) -> None:
        """贴在角色头顶上方；对白正在显示时抬到它上面——番茄钟压对白，对白压角色。"""
        point = self.pet.frameGeometry().topLeft() - QPoint(0, self.height() + SPEECH_TIMER_GAP)
        speech = getattr(self.pet, 'speech', None)
        if speech is not None and speech.isVisible():
            point = QPoint(point.x(), min(point.y(), speech.y() - self.height() - SPEECH_TIMER_GAP))
        self.move(point)

    def set_paused(self, paused: bool) -> None:
        """按钮文字跟着状态走：暂停中显示「继续」。"""
        self.pause.setText("继续" if paused else "暂停")


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

    def set_mood(self, state: str | None) -> None:
        """情绪表情：待机时露脸，走路 / 拖拽 / 说话时让位给各自的素材。"""
        if self.has_sprite:
            self.animator.set_mood(state)

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
    def __init__(self, open_settings: Callable[[], None], refresh_settings: Callable[[dict], None], notify: Callable[[str, str], None] | None = None) -> None:
        super().__init__(); self.open_settings = open_settings; self.refresh_settings = refresh_settings; self.notify = notify; self.drag: QPoint | None = None; self._drag_direction = DragDirection(); self.remaining = 0; self._pomodoro_total = 0; self.following = False; self._think_until = 0.0; self._menu_open = False; self._reply_form = FORM_WINDOW; self._speech_buffer = ''; self._speech_shown = ''; self._drag_origin = QPoint(0, 0); self.assets: CharacterAssets | None = None; self.sprite_size = DEFAULT_CANVAS_SIZE; self.activity = ACTIVITY_DEFAULT; self.pinned_expression: str | None = None; self._expression_menu: QMenu | None = None; self._art_warned: str | None = None; self._auto_expression = True; self._reasoning = False; self._mood_state: str | None = None; self._mood_changed_at = -10 ** 9; self._mood_dwell_ms, self._mood_timeout_ms = mood_timing(DEFAULT_SENSITIVITY)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint); self.setAttribute(Qt.WA_TranslucentBackground)
        self.canvas = PetCanvas(self); self.bubble = QLabel(self); self.bubble.setAlignment(Qt.AlignCenter); self.bubble.setStyleSheet('background:rgba(255,255,255,235); color:#645C73; border:1px solid #EEEAF6; border-radius:12px; font-size:11px;'); self.bubble.hide()
        self._sync_geometry()
        self.animation = QPropertyAnimation(self, b'pos', self); self.animation.finished.connect(self._on_wander_finished)
        self.conversation = ConversationService(self); self.conversation.busy_changed.connect(self._on_conversation_busy); self.conversation.reasoning_changed.connect(self._on_reasoning_changed)
        self.conversation.consolidated.connect(self._on_consolidated)
        self.conversation.mood_changed.connect(self._on_mood_changed)
        # 对白气泡形式下，回复也往角色上方那个小窗口里流一份（聊天窗口照旧记录，两者同一段会话）
        self.conversation.delta.connect(self._on_reply_delta)
        self.conversation.finished.connect(self._on_reply_finished)
        self.conversation.failed.connect(self._on_reply_failed)
        self.conversation.silenced.connect(self._on_reply_silenced)
        self.chat = ChatDialog(None, self.conversation); self.speech = SpeechBubble(self); self.input_box = FloatingInput(self); self.notes = NotesWindow(self, self._on_notes_write); self.input_box.submitted.connect(self._send_from_input); self.conversation.busy_changed.connect(self.input_box.set_busy); self._speech_timer = QTimer(self); self._speech_timer.setInterval(SPEECH_FLUSH_MS); self._speech_timer.timeout.connect(self._flush_speech); self.timer_window = TimerWindow(self, self.toggle_pomodoro, self.reset_pomodoro, self.stop_pomodoro); self.tick = QTimer(self); self.tick.timeout.connect(self._tick); self.wander = QTimer(self); self.wander.setInterval(5500); self.wander.timeout.connect(self._wander); self.wander.start(); self._follow_timer = QTimer(self); self._follow_timer.setInterval(FOLLOW_TICK_MS); self._follow_timer.timeout.connect(self._follow_step); self._mood_timer = QTimer(self); self._mood_timer.setSingleShot(True); self._mood_timer.timeout.connect(self._expire_mood); self.load_character(load_settings()); self._place(); self._resume_motion(); self._topmost_timer = QTimer(self); self._topmost_timer.setInterval(TOPMOST_CHECK_MS); self._topmost_timer.timeout.connect(self._keep_on_top); self._topmost_timer.start()
    def _place(self) -> None:
        area = self.screen().availableGeometry(); self.move(area.right()-self.width()-24, area.bottom()-self.height()-20)
    def _sync_geometry(self) -> None:
        if self.assets is not None:
            width = max(120, round(SPRITE_HEIGHT * self.sprite_size.width() / self.sprite_size.height())); height = SPRITE_HEIGHT
        else:
            width, height = DEFAULT_CANVAS_SIZE.width(), DEFAULT_CANVAS_SIZE.height()
        self.setFixedSize(width, height + BUBBLE_HEIGHT); self.canvas.setGeometry(0, BUBBLE_HEIGHT, width, height); self.bubble.setGeometry(8, 0, width - 16, BUBBLE_HEIGHT)
        if self.isVisible(): self._clamp_to_screen()
        if hasattr(self, 'speech'): self._place_floating()
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
        # 素材换了，之前那张情绪脸已经不存在，同步清掉
        self._mood_timer.stop(); self._mood_state = None
        name, info = current_character(data)
        # 自动表情开关与灵敏度跟随设置（启动、换角色、保存设置都会走到这里）
        self.set_auto_expression(
            bool(data.get('conversation', {}).get('auto_expression', True)),
            data.get('conversation', {}).get('expression_sensitivity', DEFAULT_SENSITIVITY),
        )
        # 开发者面板的开关跟随设置（启动、换角色、保存设置都会走到这里）
        devtools.set_enabled(bool(data.get("developer", {}).get("enabled", False)))
        self.conversation.configure(name, info, data.get("conversation", {}).get("base_prompt", ""))
        self.chat.set_title(name)
        self.chat.load_history(self.conversation.history(HISTORY_LIMIT))
        # 对话形式与对白透明度跟随设置（启动、换角色、保存设置都会走到这里）
        self._reply_form = str(data.get('conversation', {}).get('reply_form', FORM_WINDOW))
        self.speech.set_opacity(int(data.get('conversation', {}).get('bubble_opacity', SPEECH_OPACITY_DEFAULT)))
        self.notes.set_opacity(int(data.get('tools', {}).get('notes_opacity', NOTES_OPACITY_DEFAULT)))
    def _on_conversation_busy(self, busy: bool) -> None:
        # 刚发出消息、还一个分片都没到的时候，先按"思考"处理
        if busy: self._reasoning = True; self.canvas.set_state('Think'); self.animation.stop()
        else: self._reasoning = False; self._restore_state()
    def _on_reasoning_changed(self, reasoning: bool) -> None:
        """收到推理内容 = 模型还在想；正文开始流 = 模型开始说。

        推理模型可能几十秒只吐 reasoning_content，这段时间立绘一直"思考"；
        正文一出来就切成说话动作——看一眼动作就知道模型在想还是在说。
        """
        if not self.conversation.busy: return
        self._reasoning = reasoning
        if reasoning: self.animation.stop()
        self.canvas.set_state('Think' if reasoning else 'Talk')
    def begin_new_session(self) -> None:
        """启动时开一段全新会话：历史靠长期记忆承载，对话窗从空开始。"""
        self.conversation.start_new_session(); self.chat.load_history([])
    def reset_memory(self) -> None:
        """记忆被清空后：另起一段新会话，并把对话窗里的历史一并抹掉。"""
        self.conversation.reset_memory(); self.chat.load_history([]); self.speech.dismiss(); self.say('记忆已清空')
    def _on_consolidated(self, added: int, merged: int) -> None:
        if added > 0: self.say(f'记忆已整理，新增 {added} 条')
    def show_pet(self) -> None:
        """入口调用：有立绘就显示窗口，没有则隐藏并提示一次。"""
        if self.assets is not None:
            self._art_warned = None; self.show(); return
        self.hide(); self.speech.dismiss(); self.input_box.hide(); self.notes.hide()
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
    def set_auto_expression(self, enabled: bool, sensitivity: int = DEFAULT_SENSITIVITY) -> None:
        """开启自动表情，并按灵敏度换算切换节奏（见 app/pet/emotion.py 的参数表）。"""
        was_enabled = self._auto_expression
        self._auto_expression = bool(enabled)
        self._mood_dwell_ms, self._mood_timeout_ms = mood_timing(sensitivity)
        self.conversation.set_auto_expression(bool(enabled))
        if was_enabled and not enabled: self._clear_mood()
    def _on_mood_changed(self, state: str | None) -> None:
        """模型在回复里标出的表情：立刻生效，超时没有新情绪就回到默认立绘。"""
        if not self._auto_expression: return
        if state != self._mood_state:
            # 最短停留：刚换过就不再跟着下一句翻，避免短句连发时表情来回抽动
            if (time.monotonic() - self._mood_changed_at) * 1000 < self._mood_dwell_ms: return
            self._mood_state = state; self._mood_changed_at = time.monotonic(); self.canvas.set_mood(state)
        self._mood_timer.start(self._mood_timeout_ms)
    def _expire_mood(self) -> None:
        self._clear_mood()
    def _clear_mood(self) -> None:
        self._mood_timer.stop()
        self._mood_state = None
        self.canvas.set_mood(None)
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
    def _thinking(self) -> bool:
        """现在是不是正摆着"思考"动作：推理中，或刚弹过一句气泡（气泡配的也是 Think）。

        这时候一律站着不动——一动起来，思考动作就被 Move 素材盖住了，看着像边发呆边挪。
        """
        return self._reasoning or time.monotonic() < self._think_until
    def _keep_on_top(self) -> None:
        """定时复核"桌宠在最上面"。

        别的程序（尤其是全屏程序来回切）会把置顶属性顶掉，之后桌宠就沉到别人窗口后面
        去了。这里重新声明一次，并带上番茄钟浮窗——两层都声明，相对关系（浮窗在角色
        之上）才始终一致。菜单弹出期间跳过：否则桌宠会被提到菜单上面，菜单就点不着了。
        """
        if not self.isVisible() or self.drag or self._menu_open: return
        _reassert_topmost(self)
        if self.timer_window.isVisible(): _reassert_topmost(self.timer_window)
    def _on_wander_finished(self) -> None:
        # 拖拽中不要覆盖 Drag 姿态（松手时 mouseReleaseEvent 会自己恢复）
        if not self.drag: self._restore_state()
        # 跟随中：这一步走完立刻接着下一步，中间不留停顿
        if self.following: QTimer.singleShot(0, self._follow_step)
    def _build_motion_menu(self, menu: QMenu) -> None:
        """「动作」面板：固定 / 移动 / 跟随鼠标，三选一。"""
        # 和表情子菜单一样必须持有引用：addMenu(str) 返回的子菜单会被 PySide 回收
        submenu = styled_menu(menu, '动作')
        menu.addMenu(submenu); self._motion_menu = submenu
        group = QActionGroup(submenu); group.setExclusive(True)
        mode = str(load_settings()['motion']['mode'])
        for label, value in (('固定模式','stationary'), ('自由移动','movable'), ('跟随鼠标','follow')):
            action = submenu.addAction(label); action.setCheckable(True); action.setChecked(mode==value)
            action.triggered.connect(lambda checked=False, target=value: self.set_motion_mode(target))
            group.addAction(action)
    def _build_context_menu(self) -> QMenu:
        d=load_settings(); m=styled_menu(self)
        self._build_expression_menu(m)
        self._build_motion_menu(m)
        m.addSeparator()
        self._add_conversation_action(m); self._add_notes_action(m); a=m.addAction('停止番茄钟' if self.pomodoro_active else f"开始 {d['tools']['pomodoro_minutes']} 分钟番茄钟"); a.triggered.connect(self.stop_pomodoro if self.pomodoro_active else self.start_pomodoro); m.addSeparator(); a=m.addAction('设置'); a.triggered.connect(self.open_settings)
        return m
    def contextMenuEvent(self, e: QContextMenuEvent) -> None:
        # 菜单弹出期间挂个标记：看门狗这会儿不要重声明置顶，否则桌宠被提到菜单之上，
        # 菜单就被自己的角色挡住了，点不着
        self._menu_open = True
        try: self._build_context_menu().exec(e.globalPos())
        finally: self._menu_open = False
    def mousePressEvent(self,e:QMouseEvent)->None:
        if e.button()==Qt.LeftButton:
            # 抓住角色时先停掉随机游走：否则动画会继续驱动窗口位置，和拖拽互相拉扯
            self.animation.stop(); self._drag_direction.reset()
            self._drag_origin=self.pos()
            self.drag=e.globalPosition().toPoint()-self.frameGeometry().topLeft(); self.canvas.set_state('Drag')
    def mouseMoveEvent(self,e:QMouseEvent)->None:
        if self.drag and e.buttons()&Qt.LeftButton:
            position=e.globalPosition().toPoint()-self.drag
            self.canvas.set_state('Drag', mirrored=self._drag_direction.update(position.x()-self.x())); self.move(position); self.timer_window.place()
    def mouseReleaseEvent(self,e:QMouseEvent)->None:
        if e.button()==Qt.LeftButton:
            self.drag=None; self._restore_state()
            moved=self.pos()-self._drag_origin
            # 只把"真搬动了"算事件（见 DRAG_EVENT_MIN_PIXELS）：它会真的打一次接口。
            # 不报位置：那会在上下文里堆一串过时的方位，模型也用不上
            if math.hypot(moved.x(),moved.y())>=DRAG_EVENT_MIN_PIXELS:
                self.conversation.notify_event('拖动', '用户拖动了你')
    def hideEvent(self, event)->None:  # type: ignore[no-untyped-def]
        # 角色被藏起来时，挂在它身边的小窗口跟着走：它们都是独立窗口，不会自动跟随。
        # 番茄钟浮窗除外——它还代表着正在跑的计时，不该被一起收掉。
        for name in ('speech', 'input_box', 'notes'):
            widget = getattr(self, name, None)
            if widget is not None: widget.hide()
        super().hideEvent(event)
    def moveEvent(self,e)->None:  # type: ignore[no-untyped-def]
        if hasattr(self,'timer_window') and self.timer_window.isVisible(): self.timer_window.place()
        if hasattr(self,'speech'): self._place_floating()
    def set_motion_mode(self, mode: str, notice: bool = True)->None:
        """切换动作模式：``stationary`` 固定 / ``movable`` 自由走动 / ``follow`` 跟随鼠标。

        ``notice=False`` 用于启动时静默落回默认模式，不弹气泡。
        """
        if mode not in ('stationary','movable','follow'): mode='stationary'
        was=str(load_settings()['motion']['mode'])
        d=load_settings(); d['motion']['mode']=mode; save_settings(d)
        self.following = mode=='follow'
        if self.following:
            self._follow_timer.start(FOLLOW_TICK_MS); self._follow_step()
        else:
            self._follow_timer.stop(); self.animation.stop(); self._restore_state()
        # 不弹"固定在这里 / 自由走动 / 跟着你走"这类提示：它和角色上方的对白挤在同一片
        # 地方，一旦模型顺着事件回上一句，两条文字就叠在一起了。切换本身看得见（菜单勾选、
        # 角色开始 / 停止走动），三种模式都作为 [动作] 事件交给模型，由它决定要不要开口。
        # 启动时静默落回默认模式不发事件，见 notice。
        if not notice or mode == was: return
        self.conversation.notify_event('动作', {
            'movable':'用户让你开始自由走动',
            'stationary':'用户把你固定在了原地',
            'follow':'用户让你开始跟着鼠标走',
        }[mode])
    def exit_follow(self)->None:
        """退出跟随（右键里取消）：原地停下，回到固定模式。"""
        if self.following: self.set_motion_mode('stationary')
    def _resume_motion(self)->None:
        """每次启动都回到「自由移动」。

        跟随是"追着光标跑"，上次没退出就再启动会显得莫名其妙；固定模式点一次菜单就有，
        也不指望它被记住。所以启动时一律落到自由移动，并写回配置——右键菜单的勾选和
        随机游走都读这份配置。
        """
        self.set_motion_mode('movable', notice=False)
    def _follow_standoff(self)->float:
        """停下时离鼠标多远：窗口半对角线 + 一点余量。

        用半对角线（而不是宽或高的一半）：不管从哪个方向靠近，这个距离都能让鼠标
        落在窗口外面——停下时不压住鼠标，也就不会挡住鼠标的点击。
        """
        return math.hypot(self.width()/2, self.height()/2) + FOLLOW_MARGIN
    def _follow_step(self)->None:
        """朝鼠标走一步：速度沿用 MOVE_SPEED，走到离线一段距离就停住。"""
        if not self.following or self.drag or self.pinned_expression is not None: return
        # 番茄钟开着不动：专注期间不该跟着光标满桌跑（跟随机游走一个道理）
        if self.pomodoro_active: return
        # 思考动作期间不动（跟随机游走一个道理）
        if self._thinking(): return
        if self.animation.state()==QPropertyAnimation.State.Running: return
        cursor=QCursor.pos(); center=self.frameGeometry().center()
        dx=cursor.x()-center.x(); dy=cursor.y()-center.y(); distance=math.hypot(dx,dy); standoff=self._follow_standoff()
        if distance<=standoff: return
        step=min(distance-standoff, MOVE_SPEED*FOLLOW_STEP_MS/1000)
        if step<1: return
        area=self.screen().availableGeometry(); target=self.pos()
        # 逼近到"再走一步就会压住鼠标"为止：鼠标贴着屏幕边缘时，理想停点会被屏幕边界
        # 挤回来，宁可就地停住也不能让窗口盖住鼠标——盖住就点不动了。
        for _ in range(6):
            ratio=step/distance
            point=QPoint(round(self.x()+dx*ratio), round(self.y()+dy*ratio))
            point=QPoint(max(area.left(),min(area.right()-self.width(),point.x())), max(area.top(),min(area.bottom()-self.height(),point.y())))
            if not (point.x()<=cursor.x()<point.x()+self.width() and point.y()<=cursor.y()<point.y()+self.height()):
                target=point; break
            step*=0.7
        moved=math.hypot(target.x()-self.x(), target.y()-self.y())
        if moved<2: return
        # 时长按**实际**位移折算：不管是被屏幕边界夹过还是缩短过，速度都还是 MOVE_SPEED
        self.canvas.set_state('Move', mirrored=wander_mirrored(target.x()-self.x())); self.animation.stop()
        self.animation.setStartValue(self.pos()); self.animation.setEndValue(target)
        self.animation.setDuration(max(80, round(moved/MOVE_SPEED*1000))); self.animation.start()
    def apply_settings(self, data:dict)->None:
        # 对话窗/输入框的开关已取消：只由右键菜单打开；保存设置只做一件事——
        # 换了对话形式就把另一种收起来，免得两个入口同时挂着
        self.load_character(data); self.show_pet()
        if self._reply_form == FORM_BUBBLE: self.chat.hide()
        else: self.input_box.hide()
        if self.isVisible(): self.say('设置已保存')
    # ---- 对话形式：聊天窗口 / 漂浮输入框 + 对白气泡 -------------------------

    def _speech_hold_ms(self) -> int:
        """对白停留时长（秒 → 毫秒）：设置里 3~30 秒，越界就夹回来。"""
        try: seconds = int(load_settings()['conversation'].get('bubble_seconds', 8))
        except (TypeError, ValueError): seconds = 8
        return max(3, min(30, seconds)) * 1000
    def _on_reply_delta(self, text: str) -> None:
        if self._reply_form != FORM_BUBBLE: return
        self._speech_buffer += text
        if not self._speech_timer.isActive(): self._speech_timer.start()
    def _flush_speech(self) -> None:
        text = self._speech_buffer.strip()
        if not text or text == self._speech_shown: return
        self._speech_shown = text
        self.speech.show_text(text)          # 流式期间只长个儿，不启动消失计时
    def _on_reply_finished(self, text: str) -> None:
        self._speech_timer.stop()
        if self._reply_form != FORM_BUBBLE: return
        body = (text or self._speech_buffer).strip() or '...'
        self._speech_buffer = ''; self._speech_shown = body
        self.speech.show_text(body, hold_ms=self._speech_hold_ms())
    def _on_reply_silenced(self) -> None:
        """模型对事件选择"不作回应"：界面什么都不显示（历史与日志里照旧留着这一轮）。"""
        self._speech_timer.stop(); self._speech_buffer = ''; self._speech_shown = ''
    def _on_reply_failed(self, message: str) -> None:
        self._speech_timer.stop(); self._speech_buffer = ''; self._speech_shown = ''
        if self._reply_form != FORM_BUBBLE: return
        self.speech.show_text(f'出错：{message}', hold_ms=self._speech_hold_ms())
    def _send_from_input(self, text: str) -> None:
        """漂浮输入框发出去的消息。

        同一段会话：照样往聊天窗口里补一条用户气泡——它同样挂着这段会话，如此之后
        再打开聊天窗口时历史是连着的，不会变成两条互不相干的对话。
        """
        self.chat.add(text, True)
        self.conversation.send(text)
    def open_input(self) -> None:
        """托盘入口：打开当前这个对话形式。"""
        if self._reply_form == FORM_BUBBLE: self.input_box.open()
        else: self._open_chat()
    def notes_position(self) -> str:
        """记事板每次打开的落点：角色旁 / 右下角 / 屏幕中间（设置 → 工具）。"""
        return str(load_settings().get('tools', {}).get('notes_position', NOTES_POSITION_PET))
    def notes_point(self, size: QSize, near: QPoint | None = None) -> QPoint:
        """算出生记事板的落点，并统一收进可见区域。

        只有记事板吃这项设置——对话窗与漂浮输入框始终贴着角色，它们跟对话形式绑定，
        挪到屏幕角落反而别扭。``near`` 是「角色旁」时的理想位置（由记事板自己给）。
        """
        area = self.screen().availableGeometry()
        mode = self.notes_position()
        if mode == NOTES_POSITION_CORNER:
            point = QPoint(area.right() - size.width() - WINDOW_MARGIN, area.bottom() - size.height() - WINDOW_MARGIN)
        elif mode == NOTES_POSITION_CENTER:
            point = QPoint(area.center().x() - size.width() // 2, area.center().y() - size.height() // 2)
        else:
            point = near if near is not None else self.frameGeometry().topLeft() - QPoint(size.width() + WINDOW_MARGIN, 150)
        x = max(area.left() + 8, min(point.x(), area.right() - size.width() - 8))
        y = max(area.top() + 8, min(point.y(), area.bottom() - size.height() - 8))
        return QPoint(x, y)
    def _open_chat(self) -> None:
        """打开对话窗：贴在角色左侧（位置与「记事板窗口位置」无关）。"""
        area = self.screen().availableGeometry()
        size = self.chat.size()
        point = self.frameGeometry().topLeft() - QPoint(size.width() + WINDOW_MARGIN, 150)
        x = max(area.left() + 8, min(point.x(), area.right() - size.width() - 8))
        y = max(area.top() + 8, min(point.y(), area.bottom() - size.height() - 8))
        self.chat.show_at(QPoint(x, y))
    def toggle_input(self) -> None:
        """右键入口：漂浮输入框开 / 收。"""
        if self._reply_form != FORM_BUBBLE:
            self.chat.hide() if self.chat.isVisible() else self._open_chat(); return
        self.input_box.hide() if self.input_box.isVisible() else self.input_box.open()
    def _add_conversation_action(self, menu: QMenu) -> None:
        """右键菜单里的"打开对话"入口，按当前形式决定开哪一个。"""
        if self._reply_form == FORM_BUBBLE:
            opened = self.input_box.isVisible()
            action = menu.addAction('收起输入框' if opened else '打开输入框'); action.triggered.connect(self.toggle_input)
            return
        opened = self.chat.isVisible()
        action = menu.addAction('关闭对话框' if opened else '打开对话框')
        action.triggered.connect(self.close_chat if opened else self._open_chat)
    def _add_notes_action(self, menu: QMenu) -> None:
        """右键菜单里的记事板入口。"""
        opened = self.notes.isVisible()
        action = menu.addAction('收起记事板' if opened else '打开记事板')
        action.triggered.connect(self.toggle_notes)
    def toggle_notes(self) -> None:
        """记事板开 / 收。"""
        self.notes.hide() if self.notes.isVisible() else self.notes.open()
    def _on_notes_write(self) -> None:
        """记事板写入了：只告诉模型"有人在记事板上写了字"，正文不出门。

        冷却交给事件层（``service.EVENT_COOLDOWN`` 里记事板是 10 秒），所以连写一阵
        也只会偶尔提一次。
        """
        self.conversation.notify_event('记事板', '用户在记事板上写了几笔（你看不到具体内容）')
    def _place_floating(self) -> None:
        """跟随角色移动：对白在角色上方、输入框在角色下方。"""
        if hasattr(self, 'speech') and self.speech.isVisible(): self.speech.place()
        if hasattr(self, 'input_box') and self.input_box.isVisible(): self.input_box.place()
    def close_chat(self)->None: self.chat.hide()
    @property
    def pomodoro_active(self) -> bool:
        """番茄钟开着（含暂停）：浮窗还显示着就算，暂停时不该变回"开始"。"""
        return self.timer_window.isVisible()
    def start_pomodoro(self)->None:
        self._pomodoro_total=load_settings()['tools']['pomodoro_minutes']*60; self.remaining=self._pomodoro_total
        self.timer_window.set_paused(False); self.tick.start(1000); self._timer_text(); self.timer_window.place(); self.timer_window.show(); self.canvas.set_state('Focus'); self.say('开始专注')
        self.conversation.notify_event('番茄钟', f'用户开始了一段 {self._pomodoro_total//60} 分钟的专注计时')
    def toggle_pomodoro(self)->None:
        """暂停 / 继续。浮窗不隐藏——它同时是"继续"的入口。"""
        if self.tick.isActive():
            self.tick.stop(); self.timer_window.set_paused(True); self._restore_state(); self.say('番茄钟已暂停')
            self.conversation.notify_event('番茄钟', '用户暂停了专注计时')
        else:
            self.tick.start(1000); self.timer_window.set_paused(False); self.canvas.set_state('Focus'); self.say('继续专注')
            self.conversation.notify_event('番茄钟', '用户继续了专注计时')
    def reset_pomodoro(self)->None:
        """回到设定时长并停下（浮窗留着，随时可以再点继续）。"""
        self.tick.stop(); self._pomodoro_total=load_settings()['tools']['pomodoro_minutes']*60; self.remaining=self._pomodoro_total
        self._timer_text(); self.timer_window.set_paused(False); self._restore_state(); self.say('番茄钟已重置')
    def stop_pomodoro(self)->None:
        self.tick.stop(); self.timer_window.hide(); self.timer_window.set_paused(False); self._restore_state(); self.say('番茄钟已停止')
        self.conversation.notify_event('番茄钟', '用户停止了专注计时')
    def _timer_text(self)->None: self.timer_window.label.setText(f'{self.remaining//60:02d}:{self.remaining%60:02d}')
    def _tick(self)->None:
        self.remaining-=1; self._timer_text()
        if self.remaining<=0:
            self.tick.stop(); self.timer_window.hide(); self.timer_window.set_paused(False); self._restore_state(); self.say('专注完成！',5000)
            if self.notify is not None: self.notify('番茄钟结束', f'{max(1,self._pomodoro_total//60)} 分钟专注完成，起来动一动吧～')
            self.conversation.notify_event('番茄钟', f'用户 {max(1,self._pomodoro_total//60)} 分钟的专注计时完成了')
    def _wander(self)->None:
        if self.pinned_expression is not None or load_settings()['motion']['mode']!='movable' or self.drag: return
        # 番茄钟开着就老实待着：这段是"专注"时间，不该满桌乱走
        if self.pomodoro_active: return
        # 思考动作期间不随机移动：一走路，思考动作就被 Move 素材盖住了
        if self._thinking(): return
        _, chance, distance = motion_profile(self.activity)
        if self._mood_state is not None: chance *= MOOD_MOVE_DAMPING
        if random.random()>chance: return
        area=self.screen().availableGeometry(); target=QPoint(max(area.left(),min(area.right()-self.width(),self.x()+random.randint(-distance,distance))),max(area.top(),min(area.bottom()-self.height(),self.y()+random.randint(-distance//2,distance//2)))); self.canvas.set_state('Move', mirrored=wander_mirrored(target.x()-self.x())); self.animation.stop(); self.animation.setStartValue(self.pos()); self.animation.setEndValue(target); self.animation.setDuration(move_duration(self.pos(), target)); self.animation.start()
    def say(self,text:str,duration:int=SAY_MS)->None:
        # 气泡都是"在处理 / 状态提示"，一律配思考动作；说话动作只留给模型正文输出。
        # 摆着思考动作时一律不动（跟推理时同一套规则），所以顺手停掉当前这段移动。
        self._think_until=time.monotonic()+duration/1000; self.animation.stop()
        self.bubble.setText(text); self.bubble.show(); QTimer.singleShot(duration,self.bubble.hide); self.canvas.show_temporary('Think',duration)





