"""A lightweight, code-drawn desktop pet placeholder."""
from __future__ import annotations

import random
from typing import Callable

from PySide6.QtCore import QPropertyAnimation, QPoint, QTimer, Qt
from PySide6.QtGui import QColor, QContextMenuEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QVBoxLayout, QWidget

from app.config import load_settings, save_settings


class ChatDialog(QDialog):
    """Temporary floating chat shell until an AI provider is connected."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("和碎碎聊天")
        self.setFixedSize(360, 430)
        self.setStyleSheet("QDialog { background: white; } QLabel { color: #5B5668; } QLineEdit { border: 1px solid #E8E5F0; border-radius: 10px; padding: 9px; } QPushButton { background: #917DE8; color: white; border: 0; border-radius: 10px; padding: 9px 16px; }")
        layout = QVBoxLayout(self)
        title = QLabel("碎碎")
        title.setStyleSheet("font-size: 18px; font-weight: 600; color: #413C56;")
        layout.addWidget(title)
        self.history = QLabel("你好！对话窗口已准备好。\n\n配置 AI 模型后，这里会显示真实回复。")
        self.history.setWordWrap(True)
        self.history.setAlignment(Qt.AlignTop)
        self.history.setStyleSheet("background: #FAF9FD; border-radius: 12px; padding: 14px;")
        layout.addWidget(self.history, 1)
        row = QHBoxLayout()
        self.input = QLineEdit(placeholderText="和碎碎说点什么……")
        send = QPushButton("发送")
        send.clicked.connect(self.send_message)
        row.addWidget(self.input, 1); row.addWidget(send)
        layout.addLayout(row)

    def send_message(self) -> None:
        message = self.input.text().strip()
        if not message:
            return
        self.history.setText(f"你：{message}\n\n碎碎：我收到了。配置 AI 后，我会认真回答你。")
        self.input.clear()


class PetCanvas(QWidget):
    """Simple line-drawn pet used while Live2D assets are unavailable."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.is_acting = False

    def play_action(self) -> None:
        self.is_acting = True
        self.update()
        QTimer.singleShot(650, self.stop_action)

    def stop_action(self) -> None:
        self.is_acting = False
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        ink = QColor("#665B82")
        soft = QColor("#F1EDFF")
        painter.setPen(QPen(ink, 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(soft)
        painter.drawEllipse(26, 24, 116, 108)
        painter.drawEllipse(48, 119, 72, 48)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(59, 67, 8, 9)
        painter.drawEllipse(102, 67, 8, 9)
        painter.drawArc(75, 78, 20, 18, 15 * 15, 150 * 16)
        painter.drawLine(46, 38, 35, 14)
        painter.drawLine(122, 38, 134, 14)
        painter.drawLine(49, 130, 27, 145 if self.is_acting else 138)
        painter.drawLine(119, 130, 142, 112 if self.is_acting else 138)
        painter.end()


class PetWindow(QWidget):
    """Always-on-top desktop pet with drag, quick menu and autonomous movement."""

    def __init__(self, open_settings: Callable[[], None]) -> None:
        super().__init__()
        self.open_settings = open_settings
        self.drag_offset: QPoint | None = None
        self.move_animation: QPropertyAnimation | None = None
        self.chat = ChatDialog(self)
        self.remaining_seconds = 0
        self.setFixedSize(168, 192)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.canvas = PetCanvas(self)
        self.canvas.setGeometry(0, 22, 168, 168)
        self.bubble = QLabel(self)
        self.bubble.setGeometry(10, 0, 148, 35)
        self.bubble.setAlignment(Qt.AlignCenter)
        self.bubble.setStyleSheet("background: rgba(255,255,255,235); color: #645C73; border: 1px solid #EEEAF6; border-radius: 12px; font-size: 11px;")
        self.bubble.hide()
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._tick_pomodoro)
        self.wander_timer = QTimer(self)
        self.wander_timer.setInterval(5500)
        self.wander_timer.timeout.connect(self._try_wander)
        self.wander_timer.start()
        self._place_bottom_right()

    def _place_bottom_right(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.move(area.right() - self.width() - 24, area.bottom() - self.height() - 20)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        data = load_settings()
        movable = data["motion"]["mode"] == "movable"
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background: white; border: 1px solid #E9E5F1; border-radius: 10px; padding: 6px; } QMenu::item { padding: 8px 34px 8px 12px; border-radius: 7px; color: #4D485A; } QMenu::item:selected { background: #F0EDFB; color: #6D5DC0; } QMenu::separator { height: 1px; background: #EEEAF4; margin: 5px 8px; }")
        mode = menu.addAction("切换为固定模式" if movable else "切换为自由模式")
        mode.triggered.connect(self.toggle_mode)
        menu.addSeparator()
        chat = menu.addAction("打开对话框")
        chat.triggered.connect(self.open_chat)
        pomodoro = menu.addAction(f"开始 {data['tools']['pomodoro_minutes']} 分钟番茄钟")
        pomodoro.triggered.connect(self.start_pomodoro)
        menu.addSeparator()
        settings = menu.addAction("设置")
        settings.triggered.connect(self.open_settings)
        menu.exec(event.globalPos())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.drag_offset and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.drag_offset = None
            self._say("已放好啦")
            event.accept()

    def toggle_mode(self) -> None:
        data = load_settings()
        data["motion"]["mode"] = "stationary" if data["motion"]["mode"] == "movable" else "movable"
        save_settings(data)
        self._say("固定模式" if data["motion"]["mode"] == "stationary" else "自由模式")

    def open_chat(self) -> None:
        self.chat.show()
        self.chat.activateWindow()
        self.chat.raise_()

    def start_pomodoro(self) -> None:
        minutes = load_settings()["tools"]["pomodoro_minutes"]
        self.remaining_seconds = minutes * 60
        self.tick_timer.start(1000)
        self._say(f"开始专注 {minutes} 分钟")

    def _tick_pomodoro(self) -> None:
        self.remaining_seconds -= 1
        if self.remaining_seconds <= 0:
            self.tick_timer.stop()
            self._say("专注完成，休息一下吧！", 5000)
            return
        if self.remaining_seconds % 60 == 0:
            self._say(f"还剩 {self.remaining_seconds // 60} 分钟")

    def _try_wander(self) -> None:
        if load_settings()["motion"]["mode"] != "movable" or self.drag_offset or random.random() > 0.28:
            return
        self.canvas.play_action()
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            return
        area = screen.availableGeometry()
        target = QPoint(
            max(area.left(), min(area.right() - self.width(), self.x() + random.randint(-70, 70))),
            max(area.top(), min(area.bottom() - self.height(), self.y() + random.randint(-35, 35))),
        )
        self.move_animation = QPropertyAnimation(self, b"pos", self)
        self.move_animation.setDuration(650)
        self.move_animation.setStartValue(self.pos())
        self.move_animation.setEndValue(target)
        self.move_animation.start()

    def _say(self, message: str, duration: int = 2300) -> None:
        self.bubble.setText(message)
        self.bubble.show()
        QTimer.singleShot(duration, self.bubble.hide)

