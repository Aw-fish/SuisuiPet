"""Interactive desktop pet window and its floating utilities."""
from __future__ import annotations
import random
from pathlib import Path
from typing import Callable
from PySide6.QtCore import QEvent, QPropertyAnimation, QPoint, QSize, QTimer, Qt
from PySide6.QtGui import QContextMenuEvent, QFontMetrics, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget
from app import characters
from app.config import load_settings, save_settings
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


class ChatDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.drag: QPoint | None = None
        self.setFixedSize(360, 430)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        root = QFrame(objectName="chatRoot")
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.addWidget(root)
        layout = QVBoxLayout(root); layout.setContentsMargins(16, 10, 16, 16); layout.setSpacing(10)
        self.drag_bar = QFrame(objectName="dragBar"); self.drag_bar.setFixedHeight(28); self.drag_bar.setCursor(Qt.OpenHandCursor); self.drag_bar.installEventFilter(self)
        bar = QHBoxLayout(self.drag_bar); bar.setContentsMargins(0, 0, 0, 0); bar.addStretch()
        close = QPushButton("×", objectName="chatClose"); close.clicked.connect(self.hide); bar.addWidget(close); layout.addWidget(self.drag_bar)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setFrameShape(QFrame.NoFrame); self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.messages = QWidget(); self.history = QVBoxLayout(self.messages); self.history.setContentsMargins(2, 2, 2, 2); self.history.addStretch(); self.scroll.setWidget(self.messages); layout.addWidget(self.scroll, 1)
        row = QHBoxLayout(); self.input = QLineEdit(placeholderText="说点什么……"); send = QPushButton("发送", objectName="send"); send.clicked.connect(self.send); row.addWidget(self.input, 1); row.addWidget(send); layout.addLayout(row)
        self.setStyleSheet('QDialog { background: transparent; } QFrame#chatRoot { background: #FFFFFF; border:1px solid #EAE6F2; border-radius:16px; } QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; } QLineEdit { border:1px solid #E8E5F0; background:rgba(255,255,255,220); border-radius:10px; padding:9px; } QPushButton#send { background:#917DE8; color:white; border:0; border-radius:10px; padding:9px 15px; } QPushButton#chatClose { border:0; background:transparent; color:#A39CAD; font-size:17px; min-width:24px; max-width:24px; } QPushButton#chatClose:hover { color:#685D78; background:#F4F1FA; border-radius:8px; } QLabel#petMsg { background:#F7F5FB; color:#4C4759; border-radius:12px; padding:9px 12px; } QLabel#userMsg { background:#EEEAFE; color:#51457B; border-radius:12px; padding:9px 12px; }')


    def add(self, text: str, user: bool) -> None:
        label = QLabel(text, objectName="userMsg" if user else "petMsg")
        label.setWordWrap(True)
        content_width = QFontMetrics(label.font()).horizontalAdvance(text) + 25
        label.setFixedWidth(min(306, max(72, content_width)))
        label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row = QHBoxLayout()
        if user: row.addStretch(); row.addWidget(label)
        else: row.addWidget(label); row.addStretch()
        self.history.insertLayout(self.history.count() - 1, row)
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def eventFilter(self, watched, event):  # type: ignore[no-untyped-def]
        if watched is self.drag_bar:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self.drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft(); self.drag_bar.setCursor(Qt.ClosedHandCursor); return True
            if event.type() == QEvent.MouseMove and self.drag and event.buttons() & Qt.LeftButton:
                self.move(event.globalPosition().toPoint() - self.drag); return True
            if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                self.drag = None; self.drag_bar.setCursor(Qt.OpenHandCursor); return True
        return super().eventFilter(watched, event)

    def send(self) -> None:
        text = self.input.text().strip()
        if text: self.add(text, True); self.add("已收到信息，此时显示测试文本123123。", False); self.input.clear()

    def show_near(self, pet: QWidget) -> None:
        area = pet.screen().availableGeometry(); point = pet.frameGeometry().topLeft() - QPoint(self.width() + 30, 180)
        self.move(max(area.left() + 8, point.x()), max(area.top() + 8, point.y())); self.show(); self.raise_(); self.activateWindow()


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
        self.chat = ChatDialog(self); self.timer_window = TimerWindow(self, self.stop_pomodoro); self.tick = QTimer(self); self.tick.timeout.connect(self._tick); self.wander = QTimer(self); self.wander.setInterval(5500); self.wander.timeout.connect(self._wander); self.wander.start(); self.load_character(load_settings()); self._place()
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





