"""与设置界面风格一致的圆角菜单。

Qt 默认会给弹出菜单加一层方形系统投影，圆角样式会因此露出直角。
这里统一关掉投影并开启窗口透明，让 :data:`MENU_QSS` 的圆角真正生效。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMenu, QWidget

MENU_QSS = (
    'QMenu { background:white; border:1px solid #E9E5F1; border-radius:10px; padding:6px; }'
    'QMenu::item { padding:8px 34px 8px 12px; border-radius:7px; }'
    'QMenu::item:selected { background:#F0EDFB; color:#6D5DC0; }'
    'QMenu::item:disabled { color:#BEB9C9; }'
    'QMenu::separator { height:1px; background:#F0EDF5; margin:5px 8px; }'
)


def style_menu(menu: QMenu) -> QMenu:
    """给已有菜单套上圆角样式并去掉方形投影。"""
    menu.setStyleSheet(MENU_QSS)
    menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    menu.setAttribute(Qt.WA_TranslucentBackground)
    return menu


def styled_menu(parent: QWidget | None = None, title: str = "") -> QMenu:
    """创建一个圆角、无方形投影的菜单。"""
    return style_menu(QMenu(title, parent) if title else QMenu(parent))
