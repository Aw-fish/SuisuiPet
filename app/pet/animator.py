"""Frame playback driver for :class:`~app.pet.character_sprite.CharacterAssets`."""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QPixmap

from app.pet.character_sprite import CharacterAssets

#: 临时动作（说话等）的额外帧率覆盖
TEMPORARY_FPS: dict[str, int] = {"Talk": 7}


class SpriteAnimator(QObject):
    """Advances the current action's frames and supports temporary overrides.

    基础状态由 :meth:`set_state` 决定；:meth:`show_temporary` 用于说话这类
    "播放一段后自动回到基础状态"的动作，优先级高于基础状态。
    """

    frame_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._assets: CharacterAssets | None = None
        self._state = "Idle"
        self._mirrored = False
        self._index = 0
        self._override: str | None = None
        self._override_mirrored = False
        self._pinned: str | None = None
        self._mood: str | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._expire = QTimer(self)
        self._expire.setSingleShot(True)
        self._expire.timeout.connect(self.clear_temporary)

    def set_assets(self, assets: CharacterAssets | None) -> None:
        self._assets = assets
        self._override = None
        self._pinned = None
        self._mood = None
        self._index = 0
        self._expire.stop()
        self._sync_timer()
        self.frame_changed.emit()

    def pin(self, state: str) -> None:
        """固定显示某个素材（通常是整张立绘的表情），优先级高于一切。"""
        if self._assets is None or not self._assets.has(state):
            return
        self._pinned = state
        self._override = None
        self._expire.stop()
        self._index = 0
        self._sync_timer()
        self.frame_changed.emit()

    def unpin(self) -> None:
        if self._pinned is None:
            return
        self._pinned = None
        self._index = 0
        self._sync_timer()
        self.frame_changed.emit()

    @property
    def pinned(self) -> str | None:
        return self._pinned

    @property
    def mood(self) -> str | None:
        return self._mood

    def set_mood(self, state: str | None) -> None:
        """设置情绪表情（``None`` 表示回到默认立绘）。

        与 :meth:`pin` 的区别：pin 是用户手动固定，优先级最高且会暂停随机动作；
        mood 只决定"待机时用哪张脸"，走路 / 拖拽 / 说话 / 专注时仍各用各的素材，
        所以它不会让角色停下，也不会打断口型。
        """
        if state is not None and (self._assets is None or not self._assets.has(state)):
            state = None
        if state == self._mood:
            return
        self._mood = state
        self._index = 0
        self._sync_timer()
        self.frame_changed.emit()

    def set_state(self, state: str, mirrored: bool = False, restart: bool = False) -> None:
        if self._assets is None:
            return
        if state == self._state and mirrored == self._mirrored and not restart:
            return
        self._state = state
        self._mirrored = mirrored
        self._index = 0
        self._sync_timer()
        self.frame_changed.emit()

    def show_temporary(self, state: str, duration_ms: int, mirrored: bool = False) -> None:
        if self._assets is None or self._pinned is not None or not self._assets.has(state):
            return
        self._override = state
        self._override_mirrored = mirrored
        self._index = 0
        self._sync_timer()
        self._expire.start(max(240, duration_ms))
        self.frame_changed.emit()

    def clear_temporary(self) -> None:
        if self._override is None:
            return
        self._override = None
        self._index = 0
        self._sync_timer()
        self.frame_changed.emit()

    @property
    def state(self) -> str:
        return self._pinned or self._override or self._state

    def current(self) -> QPixmap | None:
        frames = self._frames()
        if not frames:
            return None
        return frames[self._index % len(frames)]

    def _display_name(self) -> str:
        """当前真正在播的素材名（用来取帧率）。"""
        if self._pinned is not None:
            return self._pinned
        if self._override is not None:
            return self._override
        if self._mood is not None and self._state == "Idle":
            return self._mood
        return self._state

    def _frames(self) -> list[QPixmap]:
        if self._assets is None:
            return []
        if self._pinned is not None:
            return self._assets.frames(self._pinned)
        if self._override is not None:
            return self._assets.frames(self._override, self._override_mirrored)
        # 情绪只在待机时露脸：走路、拖拽、说话、专注都优先播各自的素材
        if self._mood is not None and self._state == "Idle":
            return self._assets.frames(self._mood)
        return self._assets.frames(self._state, self._mirrored)

    def _interval(self) -> int:
        name = self._display_name()
        fps = TEMPORARY_FPS.get(name, CharacterAssets.fps(name))
        return max(40, round(1000 / fps))

    def _sync_timer(self) -> None:
        if len(self._frames()) <= 1:
            self._timer.stop()
            return
        self._timer.start(self._interval())

    def _advance(self) -> None:
        frames = self._frames()
        if len(frames) <= 1:
            self._timer.stop()
            return
        self._index = (self._index + 1) % len(frames)
        self.frame_changed.emit()
