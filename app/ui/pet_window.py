"""Interactive code-drawn desktop pet and its floating utilities."""
from __future__ import annotations
import random
from typing import Callable
from PySide6.QtCore import QPropertyAnimation, QPoint, QTimer, Qt
from PySide6.QtGui import QColor, QContextMenuEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget
from app.config import load_settings, save_settings


class ChatDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent); self.drag: QPoint | None = None; self.setFixedSize(360, 430); self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool); self.setAttribute(Qt.WA_TranslucentBackground)
        root = QFrame(objectName="chatRoot"); outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.addWidget(root)
        layout = QVBoxLayout(root); layout.setContentsMargins(16, 16, 16, 16); layout.setSpacing(10)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setFrameShape(QFrame.NoFrame)
        self.messages = QWidget(); self.history = QVBoxLayout(self.messages); self.history.setContentsMargins(2, 2, 2, 2); self.history.addStretch(); self.scroll.setWidget(self.messages); layout.addWidget(self.scroll, 1)
        row = QHBoxLayout(); self.input = QLineEdit(placeholderText="和碎碎说点什么……"); send = QPushButton("发送", objectName="send"); send.clicked.connect(self.send); row.addWidget(self.input, 1); row.addWidget(send); layout.addLayout(row)
        self.setStyleSheet('QFrame#chatRoot { background: rgba(255,255,255,232); border:1px solid #EAE6F2; border-radius:16px; } QScrollArea { background:transparent; } QLineEdit { border:1px solid #E8E5F0; background:rgba(255,255,255,220); border-radius:10px; padding:9px; } QPushButton#send { background:#917DE8; color:white; border:0; border-radius:10px; padding:9px 15px; } QPushButton#chatClose { border:0; background:transparent; color:#A39CAD; font-size:17px; min-width:24px; max-width:24px; } QPushButton#chatClose:hover { color:#685D78; background:#F4F1FA; border-radius:8px; } QLabel#petMsg { background:#F7F5FB; color:#4C4759; border-radius:12px; padding:9px 12px; } QLabel#userMsg { background:#EEEAFE; color:#51457B; border-radius:12px; padding:9px 12px; }')
        self.add("你好，我是碎碎。", False)

    def add(self, text: str, user: bool) -> None:
        label = QLabel(text, objectName="userMsg" if user else "petMsg"); label.setWordWrap(True); label.setMaximumWidth(245)
        row = QHBoxLayout(); row.addWidget(label, alignment=Qt.AlignRight if user else Qt.AlignLeft); self.history.insertLayout(self.history.count() - 1, row)
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton: self.drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.drag and event.buttons() & Qt.LeftButton: self.move(event.globalPosition().toPoint() - self.drag)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton: self.drag = None

    def send(self) -> None:
        text = self.input.text().strip()
        if text: self.add(text, True); self.add("我收到了。连接 AI 后，我会认真回答你。", False); self.input.clear()

    def show_near(self, pet: QWidget) -> None:
        area = pet.screen().availableGeometry(); point = pet.frameGeometry().topLeft() - QPoint(self.width() + 14, 0)
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
    def __init__(self, parent: QWidget) -> None: super().__init__(parent); self.acting = False
    def act(self) -> None: self.acting = True; self.update(); QTimer.singleShot(650, self.stop)
    def stop(self) -> None: self.acting = False; self.update()
    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing); p.setPen(QPen(QColor('#665B82'), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)); p.setBrush(QColor('#F1EDFF')); p.drawEllipse(26,24,116,108); p.drawEllipse(48,119,72,48); p.setBrush(Qt.NoBrush); p.drawEllipse(59,67,8,9); p.drawEllipse(102,67,8,9); p.drawArc(75,78,20,18,225,2400); p.drawLine(46,38,35,14); p.drawLine(122,38,134,14); p.drawLine(49,130,27,145 if self.acting else 138); p.drawLine(119,130,142,112 if self.acting else 138); p.end()


class PetWindow(QWidget):
    def __init__(self, open_settings: Callable[[], None], refresh_settings: Callable[[dict], None]) -> None:
        super().__init__(); self.open_settings = open_settings; self.refresh_settings = refresh_settings; self.drag: QPoint | None = None; self.animation: QPropertyAnimation | None = None; self.remaining = 0
        self.setFixedSize(168,192); self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint); self.setAttribute(Qt.WA_TranslucentBackground)
        self.canvas = PetCanvas(self); self.canvas.setGeometry(0,22,168,168); self.bubble = QLabel(self); self.bubble.setGeometry(10,0,148,35); self.bubble.setAlignment(Qt.AlignCenter); self.bubble.setStyleSheet('background:rgba(255,255,255,235); color:#645C73; border:1px solid #EEEAF6; border-radius:12px; font-size:11px;'); self.bubble.hide()
        self.chat = ChatDialog(self); self.timer_window = TimerWindow(self, self.stop_pomodoro); self.tick = QTimer(self); self.tick.timeout.connect(self._tick); self.wander = QTimer(self); self.wander.setInterval(5500); self.wander.timeout.connect(self._wander); self.wander.start(); self._place()
    def _place(self) -> None:
        area = self.screen().availableGeometry(); self.move(area.right()-self.width()-24, area.bottom()-self.height()-20)
    def contextMenuEvent(self, e: QContextMenuEvent) -> None:
        d=load_settings(); movable=d['motion']['mode']=='movable'; m=QMenu(self); m.setStyleSheet('QMenu { background:white; border:1px solid #E9E5F1; border-radius:10px; padding:6px; } QMenu::item { padding:8px 34px 8px 12px; border-radius:7px; } QMenu::item:selected { background:#F0EDFB; color:#6D5DC0; }')
        a=m.addAction('切换为固定模式' if movable else '切换为自由模式'); a.triggered.connect(self.toggle_mode); m.addSeparator(); a=m.addAction('打开对话框'); a.triggered.connect(lambda: self.chat.show_near(self)); a=m.addAction('停止番茄钟' if self.tick.isActive() else f"开始 {d['tools']['pomodoro_minutes']} 分钟番茄钟"); a.triggered.connect(self.stop_pomodoro if self.tick.isActive() else self.start_pomodoro); m.addSeparator(); a=m.addAction('设置'); a.triggered.connect(self.open_settings); m.exec(e.globalPos())
    def mousePressEvent(self,e:QMouseEvent)->None:
        if e.button()==Qt.LeftButton: self.drag=e.globalPosition().toPoint()-self.frameGeometry().topLeft()
    def mouseMoveEvent(self,e:QMouseEvent)->None:
        if self.drag and e.buttons()&Qt.LeftButton: self.move(e.globalPosition().toPoint()-self.drag); self.timer_window.place()
    def mouseReleaseEvent(self,e:QMouseEvent)->None:
        if e.button()==Qt.LeftButton: self.drag=None; self.say('已放好啦')
    def moveEvent(self,e)->None:  # type: ignore[no-untyped-def]
        if hasattr(self,'timer_window') and self.timer_window.isVisible(): self.timer_window.place()
    def toggle_mode(self)->None:
        d=load_settings(); d['motion']['mode']='stationary' if d['motion']['mode']=='movable' else 'movable'; save_settings(d); self.refresh_settings(d); self.say('固定模式' if d['motion']['mode']=='stationary' else '自由模式')
    def apply_settings(self, data:dict)->None: self.say('设置已保存')
    def start_pomodoro(self)->None:
        self.remaining=load_settings()['tools']['pomodoro_minutes']*60; self.tick.start(1000); self._timer_text(); self.timer_window.place(); self.timer_window.show(); self.say('开始专注')
    def stop_pomodoro(self)->None: self.tick.stop(); self.timer_window.hide(); self.say('番茄钟已停止')
    def _timer_text(self)->None: self.timer_window.label.setText(f'{self.remaining//60:02d}:{self.remaining%60:02d}')
    def _tick(self)->None:
        self.remaining-=1; self._timer_text()
        if self.remaining<=0: self.tick.stop(); self.timer_window.hide(); self.say('专注完成，休息一下吧！',5000)
    def _wander(self)->None:
        if load_settings()['motion']['mode']!='movable' or self.drag or random.random()>.28:return
        self.canvas.act(); area=self.screen().availableGeometry(); target=QPoint(max(area.left(),min(area.right()-self.width(),self.x()+random.randint(-70,70))),max(area.top(),min(area.bottom()-self.height(),self.y()+random.randint(-35,35)))); self.animation=QPropertyAnimation(self,b'pos',self); self.animation.setDuration(650); self.animation.setStartValue(self.pos()); self.animation.setEndValue(target); self.animation.start()
    def say(self,text:str,duration:int=2300)->None: self.bubble.setText(text); self.bubble.show(); QTimer.singleShot(duration,self.bubble.hide)

