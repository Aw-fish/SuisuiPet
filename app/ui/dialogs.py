"""Frameless dialogs that keep the same look as the settings window."""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

ACCENT = "#917DE8"


class WheelGuard(QObject):
    """屏蔽滚轮事件：鼠标划过数值控件时不要改动数值，太容易误触。

    设置页本身是可滚动区域，滚页面时笔尖正好压在滑块 / 数字框 / 下拉框上，
    数值就被顺手改掉了——这类"看一眼就改了"的误触比少一个便捷操作麻烦得多。
    """

    def eventFilter(self, watched, event):  # type: ignore[no-untyped-def]
        if event.type() == QEvent.Type.Wheel:
            event.ignore()
            return True
        return super().eventFilter(watched, event)


def block_wheel(owner: QWidget, *widgets: QWidget) -> WheelGuard:
    """让这些控件不吃滚轮。``owner`` 同时是拦截器的 parent，保证它不会被提前回收。"""
    guard = WheelGuard(owner)
    for widget in widgets:
        widget.installEventFilter(guard)
    return guard

#: (key, 按钮文案, 样式)；样式取值 primary / ghost / danger
Option = tuple[str, str, str]


class StyledDialog(QDialog):
    """无边框、圆角的轻量对话框，支持输入框与 1~3 个自定义按钮。"""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        message: str = "",
        *,
        input_text: str | None = None,
        placeholder: str = "",
        options: Sequence[Option] = (),
        cancel: str | None = "取消",
    ) -> None:
        super().__init__(parent)
        self._drag: QPoint | None = None
        self._choice: str | None = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setMinimumWidth(360)
        card = QFrame(objectName="card")
        self._card = card
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(QLabel(title, objectName="title"))
        if message:
            text = QLabel(message, objectName="message")
            text.setWordWrap(True)
            layout.addWidget(text)
        self.input: QLineEdit | None = None
        if input_text is not None:
            self.input = QLineEdit(input_text)
            self.input.setPlaceholderText(placeholder)
            self.input.returnPressed.connect(self._confirm)
            layout.addWidget(self.input)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch()
        if cancel is not None:
            dismiss = QPushButton(cancel, objectName="ghost")
            dismiss.clicked.connect(self.reject)
            buttons.addWidget(dismiss)
        for key, label, kind in options:
            button = QPushButton(label, objectName=kind or "primary")
            button.clicked.connect(lambda checked=False, target=key: self._choose(target))
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.setStyleSheet(_QSS)
        card.installEventFilter(self)
        self.adjustSize()
        if self.input is not None:
            self.input.setFocus()

    def _choose(self, key: str) -> None:
        self._choice = key
        self.accept()

    def _confirm(self) -> None:
        self._choose("confirm")

    def choice(self) -> str | None:
        return self._choice

    def value(self) -> str:
        return self.input.text().strip() if self.input is not None else ""

    def eventFilter(self, watched, event):  # type: ignore[no-untyped-def]
        if watched is self._card:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            elif event.type() == QEvent.Type.MouseMove and self._drag and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag)
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self._drag = None
        return super().eventFilter(watched, event)

    # ---- 常用组合 ----------------------------------------------------------

    @classmethod
    def choose(
        cls,
        parent: QWidget | None,
        title: str,
        message: str = "",
        *,
        options: Sequence[Option],
        cancel: str | None = "取消",
    ) -> str | None:
        """弹出多选一对话框，返回被点击项的 key；取消返回 None。"""
        dialog = cls(parent, title, message, options=options, cancel=cancel)
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.choice()

    @classmethod
    def ask_text(
        cls,
        parent: QWidget | None,
        title: str,
        message: str = "",
        *,
        placeholder: str = "",
        confirm: str = "确定",
        cancel: str | None = "取消",
    ) -> str | None:
        """弹出输入框，取消时返回 None。"""
        dialog = cls(
            parent,
            title,
            message,
            input_text="",
            placeholder=placeholder,
            options=(("confirm", confirm, "primary"),),
            cancel=cancel,
        )
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.value()

    @classmethod
    def confirm(
        cls,
        parent: QWidget | None,
        title: str,
        message: str = "",
        *,
        confirm: str = "确定",
        cancel: str = "取消",
        danger: bool = False,
    ) -> bool:
        """弹出确认框，只有点击确定才返回 True。"""
        kind = "danger" if danger else "primary"
        return (
            cls.choose(parent, title, message, options=(("confirm", confirm, kind),), cancel=cancel)
            == "confirm"
        )

    @classmethod
    def notice(
        cls,
        parent: QWidget | None,
        title: str,
        message: str = "",
        *,
        confirm: str = "知道了",
    ) -> None:
        """只带一个确认按钮的提示框。"""
        cls.choose(parent, title, message, options=(("ok", confirm, "primary"),), cancel=None)


_QSS = (
    'QDialog { background: transparent; }'
    'QFrame#card { background:#FFFFFF; border:1px solid #ECEAF2; border-radius:16px; }'
    'QLabel#title { color:#302C40; font:600 15px "Microsoft YaHei UI"; }'
    'QLabel#message { color:#7A7488; font:11px "Microsoft YaHei UI"; }'
    'QLineEdit { background:white; border:1px solid #E9E7EF; border-radius:9px; padding:8px 10px; color:#403C4B; min-height:20px; }'
    'QLineEdit:focus { border-color:#B8AAF3; }'
    f'QPushButton#primary {{ background:{ACCENT}; border:0; border-radius:9px; color:white; font-weight:600; padding:9px 20px; }}'
    'QPushButton#primary:hover { background:#806CD9; }'
    'QPushButton#ghost { background:#F4F1FB; border:1px solid #E9E5F5; border-radius:9px; color:#6D5DC0; font-weight:600; padding:9px 18px; }'
    'QPushButton#ghost:hover { background:#EBE6FA; }'
    'QPushButton#danger { background:#E0605F; border:0; border-radius:9px; color:white; font-weight:600; padding:9px 20px; }'
    'QPushButton#danger:hover { background:#CF5150; }'
)
