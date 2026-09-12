"""应用图标加载（由 ``tools/make_icon.py`` 生成到 ``data/``）。"""

from __future__ import annotations

from PySide6.QtGui import QIcon

from app.paths import ICON_PATH, ICON_PNG_PATH


def app_icon() -> QIcon:
    """优先用 .ico（多尺寸），缺失时回落到 .png，都没有则返回空图标。"""
    for path in (ICON_PATH, ICON_PNG_PATH):
        if path.is_file():
            return QIcon(str(path))
    return QIcon()
