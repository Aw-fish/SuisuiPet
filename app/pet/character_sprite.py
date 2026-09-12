"""Loads flat-folder sprite sets for the img character format.

素材约定：所有立绘放在同一个文件夹下，靠文件名前缀区分动画，例如
``Default.png``、``Move1.png``、``Move2.png``、``Talk1.png``、``Happy.png``。
文件名规则为 ``<动作名><可选的帧序号>``，无序号表示单帧。
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap

FRAME_PATTERN = re.compile(r"^(?P<name>[A-Za-z]+)(?P<index>\d*)$")
SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

DEFAULT_NAME = "Default"

#: 属于"动作"的素材名，其余均为整张立绘的表情
ACTION_NAMES = frozenset({"Default", "Idle", "Move", "Talk", "Focus", "Drag"})

#: 表情素材的中文显示名（未知表情直接显示原名）
EXPRESSION_LABELS: dict[str, str] = {
    "Blink": "眨眼",
    "Happy": "开心",
    "Sad": "难过",
    "Angry": "生气",
    "Surprised": "惊讶",
    "Think": "思考",
    "Normal": "平常",
}

#: 动作名 -> 兜底链，取第一个存在的素材
FALLBACKS: dict[str, tuple[str, ...]] = {
    "Idle": ("Idle", DEFAULT_NAME),
    "Move": ("Move", "Idle", DEFAULT_NAME),
    "Talk": ("Talk", "Idle", DEFAULT_NAME),
    "Focus": ("Focus", "Idle", DEFAULT_NAME),
    "Drag": ("Drag", "Idle", DEFAULT_NAME),
}

#: 扁平命名没有 manifest 承载帧率，播放速度集中定义在这里
FPS: dict[str, int] = {"Idle": 5, "Move": 10, "Talk": 9, "Focus": 4, "Drag": 8}
DEFAULT_FPS = 6


def mirror(pixmap: QPixmap) -> QPixmap:
    """精确水平镜像，避免仿射变换带来的插值模糊。"""
    image = pixmap.toImage()
    flipped = getattr(image, "flipped", None)
    if flipped is not None:
        return QPixmap.fromImage(flipped(Qt.Horizontal))
    return QPixmap.fromImage(image.mirrored(True, False))


def has_transparency(pixmap: QPixmap) -> bool:
    """桌宠立绘必须带透明背景，否则会渲染成一个白色方块。"""
    image = pixmap.toImage()
    if not image.hasAlphaChannel():
        return False
    step_x = max(1, image.width() // 32)
    step_y = max(1, image.height() // 32)
    for y in range(0, image.height(), step_y):
        for x in range(0, image.width(), step_x):
            if image.pixelColor(x, y).alpha() < 255:
                return True
    return False


def default_art_path(folder: Path | str | None) -> Path | None:
    """返回文件夹内的 Default 立绘路径。"""
    if folder is None:
        return None
    directory = Path(folder)
    if not directory.is_dir():
        return None
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith("_") or entry.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        if entry.is_file() and entry.stem.capitalize() == DEFAULT_NAME:
            return entry
    return None


def default_art_usable(folder: Path | str | None) -> bool:
    """设置界面的预校验：是否存在透明背景的 Default 立绘。"""
    path = default_art_path(folder)
    if path is None:
        return False
    pixmap = QPixmap(str(path))
    return not pixmap.isNull() and has_transparency(pixmap)


class CharacterAssets:
    """A loaded character folder: default art plus per-action frame sequences."""

    def __init__(self, folder: Path, frames: dict[str, list[QPixmap]]) -> None:
        self.folder = folder
        self.size: QSize = next(iter(frames.values()))[0].size()
        self._frames = frames
        self._mirrored: dict[str, list[QPixmap]] = {}

    @classmethod
    def load(cls, folder: Path | str | None) -> "CharacterAssets | None":
        """Scan a folder; return ``None`` when the ``Default`` art is missing."""
        if folder is None:
            return None
        directory = Path(folder)
        if not directory.is_dir():
            return None
        grouped: dict[str, list[tuple[int, Path]]] = {}
        for entry in sorted(directory.iterdir()):
            if entry.name.startswith("_") or entry.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            if not entry.is_file():
                continue
            match = FRAME_PATTERN.match(entry.stem)
            if match is None:
                continue
            name = match.group("name").capitalize()
            grouped.setdefault(name, []).append((int(match.group("index") or 0), entry))
        frames: dict[str, list[QPixmap]] = {}
        for name, entries in grouped.items():
            pixmaps = [QPixmap(str(path)) for _, path in sorted(entries, key=lambda item: item[0])]
            pixmaps = [pixmap for pixmap in pixmaps if not pixmap.isNull()]
            if pixmaps:
                frames[name] = pixmaps
        if DEFAULT_NAME not in frames or not has_transparency(frames[DEFAULT_NAME][0]):
            return None
        return cls(directory, frames)

    @property
    def default(self) -> QPixmap:
        return self._frames[DEFAULT_NAME][0]

    @property
    def actions(self) -> list[str]:
        return sorted(self._frames)

    @property
    def expressions(self) -> list[str]:
        """整张全身立绘的表情（按右键菜单展示顺序排列）。"""
        return sorted(name for name in self._frames if name not in ACTION_NAMES)

    @staticmethod
    def expression_label(name: str) -> str:
        return EXPRESSION_LABELS.get(name, name)

    def has(self, state: str) -> bool:
        return state in self._frames

    def resolve(self, state: str) -> str | None:
        for candidate in FALLBACKS.get(state, (state, DEFAULT_NAME)):
            if candidate in self._frames:
                return candidate
        return None

    def frames(self, state: str, mirrored: bool = False) -> list[QPixmap]:
        """Frames for ``state``, with fallback and cached horizontal mirroring."""
        name = self.resolve(state)
        if name is None:
            return [self.default]
        if not mirrored:
            return self._frames[name]
        if name not in self._mirrored:
            self._mirrored[name] = [mirror(pixmap) for pixmap in self._frames[name]]
        return self._mirrored[name]

    def is_animated(self, state: str) -> bool:
        name = self.resolve(state)
        return name is not None and len(self._frames[name]) > 1

    @staticmethod
    def fps(state: str) -> int:
        return FPS.get(state, DEFAULT_FPS)

    def missing(self, *states: str) -> list[str]:
        return [state for state in states if not self.has(state)]
