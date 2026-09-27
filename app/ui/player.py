"""按固定间隔播放一串音效（QtMultimedia，不引入新依赖）。

只在 **GUI 线程**里跑：技能在请求线程里通过 Qt 信号把"要演奏什么"送过来（跨线程发信号
是安全的），槽函数在这里排期播放。

每个音缓存两个播放器轮流用：像 `1 1 1` 这样的连音，只用一个播放器会把前一个音掐断，
听上去一顿一顿的。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

#: 同一个音备几个播放器（轮着用，连着的相同音才不会被切掉）
PLAYERS_PER_NOTE = 2
#: 最后一个音放完之后的收尾等待：音效长约 1 秒，比间隔长，等它响完再让动作复位
LAST_TAIL_MS = 900
#: 间隔的下限，防止模型或设置给出个 0 把播放器打爆
MIN_INTERVAL_MS = 100


class MelodyPlayer(QObject):
    """把一串音效按间隔放出来；播放期间随时可以 :meth:`stop`。"""

    #: 演奏完了（自然放完或被打断）——界面据此把动作恢复回去
    finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        #: ``音效文件 -> (播放器列表, 音频输出列表)``，两者一一对应
        self._voices: dict[Path, tuple[list[QMediaPlayer], list[QAudioOutput]]] = {}
        self._cursor: dict[Path, int] = {}
        #: 待放的一串拍；``None`` = 空拍（占一拍的时间，但什么都不放）
        self._queue: list[Path | None] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._next)
        self._interval = 500
        self._volume = 1.0
        self._playing = False

    @property
    def playing(self) -> bool:
        return self._playing

    # ---- 音量 ---------------------------------------------------------------

    def set_volume(self, volume: int) -> None:
        """音量（0~100）。已经建好的播放器也立刻跟着改。"""
        self._volume = max(0, min(100, int(volume))) / 100
        for _, outputs in self._voices.values():
            for output in outputs:
                output.setVolume(self._volume)

    # ---- 播放 ---------------------------------------------------------------

    def play(self, beats: Sequence[Path | None], interval_ms: int) -> int:
        """开始播放，返回排上了几拍。``None`` 是空拍：占一拍的时间，但什么都不放。

        上一次没放完会先停下（不打扰调用方状态）。
        """
        self._abort()
        self._queue = [
            None if beat is None or not Path(beat).is_file() else Path(beat) for beat in beats
        ]
        if not self._queue:
            return 0
        self._interval = max(MIN_INTERVAL_MS, int(interval_ms))
        self._playing = True
        total = len(self._queue)
        self._next()                      # 会立刻取走第一个音，所以数量先数好
        return total

    def stop(self) -> None:
        """停下并清空队列；只有"本来在演奏"时才通知 ``finished``。"""
        if not self._playing:
            return
        self._abort()
        self.finished.emit()

    def _abort(self) -> None:
        self._timer.stop()
        self._queue.clear()
        self._playing = False
        for players, _ in self._voices.values():
            for player in players:
                player.stop()

    def _next(self) -> None:
        """放队列里的下一拍，并排好下一次；空拍只等一拍。"""
        if not self._queue:
            self._playing = False
            self.finished.emit()
            return
        beat = self._queue.pop(0)
        if beat is not None:
            player, _ = self._voice(beat)
            player.setSource(QUrl.fromLocalFile(str(beat)))
            player.play()
        # 每次都排一次：队列空了以后到点就是"演奏完了"（空拍同样占一拍）
        self._timer.start(self._interval if self._queue else max(self._interval, LAST_TAIL_MS))

    def _voice(self, path: Path) -> tuple[QMediaPlayer, QAudioOutput]:
        """取这个音的下一个空闲播放器（没有就现建一对）。"""
        pair = self._voices.get(path)
        if pair is None:
            players: list[QMediaPlayer] = []
            outputs: list[QAudioOutput] = []
            for _ in range(PLAYERS_PER_NOTE):
                output = QAudioOutput(self)
                output.setVolume(self._volume)
                player = QMediaPlayer(self)
                player.setAudioOutput(output)
                players.append(player)
                outputs.append(output)
            pair = (players, outputs)
            self._voices[path] = pair
        index = self._cursor.get(path, 0) % len(pair[0])
        self._cursor[path] = index + 1
        return pair[0][index], pair[1][index]
