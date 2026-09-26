"""记事板窗口：随手记点东西，实时存本地。

它和桌宠只有两条关系：认桌宠当 parent（于是同属一层、跟着一起显隐），以及**写入时**
通过 ``on_write`` 告诉模型"有人在记事板上写了东西"——**正文不出门**，模型只知道这件事
发生过（想聊就自己问）。冷却交给事件层（``service.EVENT_COOLDOWN``）。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, QTimer, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app import devtools
from app.paths import NOTES_PATH

#: 记事板窗口尺寸（首次打开时的初始值，之后可以自己拖边框改）
WINDOW_SIZE = (330, 260)
#: 最小尺寸：再小就写不下几行字了
MIN_SIZE = (220, 150)
#: 鼠标离边缘这么近就当抓住了边框（像素）
RESIZE_MARGIN = 6
#: 停笔多久才落盘 / 告诉模型（毫秒）。「实时保存」没必要每个键都写一次文件。
SAVE_DELAY_MS = 500
#: 与角色之间的留白
MARGIN = 12
#: 记事板背景不透明度（百分比）的取值范围与默认值。设置项是 ``tools.notes_opacity``。
NOTES_OPACITY_MIN, NOTES_OPACITY_MAX, NOTES_OPACITY_DEFAULT = 30, 100, 90


def notes_qss(opacity: int = NOTES_OPACITY_DEFAULT) -> str:
    """记事板样式：背景不透明度按设置来（百分比 → 0~255）。"""
    alpha = max(0, min(255, round(255 * opacity / 100)))
    return (
        f'QFrame#notesRoot {{ background:rgba(255,255,255,{alpha}); border:1px solid rgba(232,228,242,{alpha}); border-radius:16px; }}'
        'QFrame#notesBar { background:transparent; }'
        'QLabel#notesTitle { color:#6D6580; font-size:12px; font-weight:600; }'
        'QPushButton#notesClose { background:transparent; color:#B4AEC4; border:0; font-size:14px; }'
        'QPushButton#notesClose:hover { color:#6D5DC0; }'
        'QTextEdit#notesText { background:transparent; border:0; color:#4B4560; font-size:12px; }'
    )


class NotesWindow(QWidget):
    """记事板：打开 / 收起，内容实时存到 ``data/notes.md``。"""

    def __init__(self, pet: QWidget, on_write: Callable[[], None]) -> None:
        super().__init__(pet)
        self.pet = pet
        self.on_write = on_write
        self._drag: QPoint | None = None
        self._loading = False
        #: 正在拖的那条边（``l`` / ``r`` / ``t`` / ``b`` 或两两组合），空串表示没在改大小
        self._resize_edge = ""
        self._resize_origin = QPoint()
        self._resize_geometry = QRect()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(MIN_SIZE[0], MIN_SIZE[1])
        self.resize(WINDOW_SIZE[0], WINDOW_SIZE[1])
        self.frame = root = QFrame(objectName="notesRoot")
        root.setMouseTracking(True)                 # 停在边缘就要能换光标，不必按住
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.addWidget(root)
        layout = QVBoxLayout(root); layout.setContentsMargins(14, 10, 14, 12); layout.setSpacing(8)
        self.bar = QFrame(objectName="notesBar"); self.bar.setFixedHeight(22); self.bar.setCursor(Qt.OpenHandCursor)
        self.bar.installEventFilter(self)
        row = QHBoxLayout(self.bar); row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("记事板", objectName="notesTitle")); row.addStretch()
        close = QPushButton("×", objectName="notesClose"); close.setFixedWidth(20)
        close.setToolTip("收起记事板"); close.clicked.connect(self.hide); row.addWidget(close)
        layout.addWidget(self.bar)
        self.text = QTextEdit(objectName="notesText")
        self.text.setPlaceholderText("随手记点什么……（自动保存）")
        self.text.textChanged.connect(self._schedule_save)
        layout.addWidget(self.text, 1)
        self.set_opacity(NOTES_OPACITY_DEFAULT)
        self._save_timer = QTimer(self); self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS); self._save_timer.timeout.connect(self.save)
        # 过滤器必须等 self.bar / self.frame 都就位再装：装早了，布局过程派发过来的事件
        # 会走进 eventFilter，而那里一上来就要访问 self.bar——异常逃进 C++ 就是进程崩溃
        self.frame.installEventFilter(self)
        self.load()

    # ---- 存取 ---------------------------------------------------------------

    def load(self) -> None:
        """从本地读回内容；文件还没建就是空的。"""
        try:
            body = NOTES_PATH.read_text(encoding="utf-8") if NOTES_PATH.is_file() else ""
        except OSError as error:
            devtools.log.warning("记事板 · 读取失败：%s", error)
            body = ""
        self._loading = True
        self.text.setPlainText(body)
        self._loading = False

    def _schedule_save(self) -> None:
        # 载入时也会触发 textChanged，那不是"用户写了字"，不该惊动任何人
        if not self._loading:
            self._save_timer.start()

    def save(self) -> None:
        """落盘，并告诉模型"有人在记事板上写了东西"（正文不出门）。"""
        self._save_timer.stop()
        try:
            NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
            NOTES_PATH.write_text(self.text.toPlainText(), encoding="utf-8")
        except OSError as error:
            devtools.log.warning("记事板 · 保存失败：%s", error)
            return
        self.on_write()

    def flush(self) -> None:
        """收起前把还没落盘的改动写下去。"""
        if self._save_timer.isActive():
            self.save()

    # ---- 窗口 ---------------------------------------------------------------

    def open(self) -> None:
        self.place(); self.show(); self.raise_(); self.text.setFocus()

    def place(self) -> None:
        """落点按「设置 → 工具 → 记事板窗口位置」：角色旁（默认，左侧）/ 右下角 / 屏幕中间。"""
        near = QPoint(
            self.pet.x() - self.width() - MARGIN,
            self.pet.y() + (self.pet.height() - self.height()) // 2,
        )
        self.move(self.pet.notes_point(QSize(self.width(), self.height()), near))

    def set_opacity(self, percent: int) -> None:
        """按设置调整背景不透明度（百分比，两端夹一下）。"""
        self.setStyleSheet(notes_qss(max(NOTES_OPACITY_MIN, min(NOTES_OPACITY_MAX, int(percent)))))

    def hideEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.flush()
        super().hideEvent(event)

    # ---- 拖边框改大小 -------------------------------------------------------

    def _edge_at(self, point: QPoint) -> str:
        """鼠标压在哪个边上（``l`` / ``r`` / ``t`` / ``b`` 或两两组合），都不沾就返回空串。"""
        vertical = 'l' if point.x() <= RESIZE_MARGIN else ('r' if point.x() >= self.width() - RESIZE_MARGIN else '')
        horizontal = 't' if point.y() <= RESIZE_MARGIN else ('b' if point.y() >= self.height() - RESIZE_MARGIN else '')
        return f'{vertical}{horizontal}'

    def _apply_cursor(self, edge: str) -> None:
        if len(edge) == 2:
            self.frame.setCursor(Qt.SizeFDiagCursor if edge in ('lt', 'rb') else Qt.SizeBDiagCursor)
        elif edge in ('l', 'r'):
            self.frame.setCursor(Qt.SizeHorCursor)
        elif edge in ('t', 'b'):
            self.frame.setCursor(Qt.SizeVerCursor)
        else:
            self.frame.unsetCursor()

    def _resize_to(self, global_point: QPoint) -> None:
        """按拖的那条边改几何：左 / 上边动的是窗口原点，位置也得跟着一起走。"""
        delta = global_point - self._resize_origin
        rect = QRect(self._resize_geometry)
        if 'l' in self._resize_edge:
            rect.setLeft(min(rect.left() + delta.x(), rect.right() - MIN_SIZE[0] + 1))
        if 'r' in self._resize_edge:
            rect.setRight(max(rect.right() + delta.x(), rect.left() + MIN_SIZE[0] - 1))
        if 't' in self._resize_edge:
            rect.setTop(min(rect.top() + delta.y(), rect.bottom() - MIN_SIZE[1] + 1))
        if 'b' in self._resize_edge:
            rect.setBottom(max(rect.bottom() + delta.y(), rect.top() + MIN_SIZE[1] - 1))
        self.setGeometry(rect)

    def eventFilter(self, watched, event):  # type: ignore[no-untyped-def]
        # 拖标题栏搬窗口（与对话窗同一套做法）
        if watched is self.bar:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft(); return True
            if event.type() == QEvent.Type.MouseMove and self._drag is not None and event.buttons() & Qt.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag); return True
            if event.type() == QEvent.Type.MouseButtonRelease and self._drag is not None:
                self._drag = None; return True
        # 边缘留白那一圈归 frame：按下时看是不是压在边上，是就进缩放
        if watched is self.frame:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.LeftButton:
                self._resize_edge = self._edge_at(event.position().toPoint())
                if self._resize_edge:
                    self._resize_origin = event.globalPosition().toPoint()
                    self._resize_geometry = self.geometry()
                    return True
            elif event.type() == QEvent.Type.MouseMove:
                if self._resize_edge and event.buttons() & Qt.LeftButton:
                    self._resize_to(event.globalPosition().toPoint())
                    return True
                self._apply_cursor(self._edge_at(event.position().toPoint()))
            elif event.type() == QEvent.Type.MouseButtonRelease and self._resize_edge:
                self._resize_edge = ""; self.frame.unsetCursor(); return True
        return super().eventFilter(watched, event)
