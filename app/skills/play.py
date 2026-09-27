"""演奏：把简谱数组交给界面，按固定间隔放对应的音效。

工具与界面分工明确：

* 本模块（跑在**请求线程**里）只做"翻译与校验"——把 1~8 的音符翻成 ``data/audio``
  里的文件、检查缺什么，然后把播放**交给注入的回调**；
* 真正的播放是 Qt 的事，必须在 GUI 线程里做，所以回调由界面提供、内部走 Qt 信号
  （跨线程发信号是安全的，见 ``app/ui/player.py``）。

文件名认三种写法（都从 ``data/audio`` 里找）：``1.mp3``、``1_do.mp3``、``do.mp3``；
高音 do（8）写 ``8`` / ``do2`` / ``do'``。缺哪个音就跳哪个音，并把缺口回报给模型。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from app import devtools, paths

#: 简谱音域：0=空拍（这一拍什么都不播，只占时间）、1=do … 8=高音 do
MIN_NOTE, MAX_NOTE = 0, 8
#: 空拍
REST = 0
#: 一次最多演奏多少拍（太长的旋律听得累，也容易把一轮对话拖很久）
MAX_NOTES = 32
#: 默认间隔（毫秒）：音效一个约 1 秒，间隔越短越连成旋律。模型没给速度时用设置里的值
DEFAULT_INTERVAL_MS = 500
#: 播放速度（每个音之间多少毫秒）的允许范围：太快糊成一团，太慢听着像一个个单音
INTERVAL_RANGE = (150, 2000)
#: 认的音频后缀（mp3 为主，wav 顺手也认）
SUFFIXES = (".mp3", ".wav", ".m4a", ".ogg")

#: 唱名 → 音高编号。按长度倒序匹配，免得 ``do2`` 被 ``do`` 抢走
ALIASES: dict[str, int] = {
    "do": 1, "re": 2, "mi": 3, "fa": 4, "so": 5, "sol": 5, "la": 6, "si": 7, "ti": 7,
    "do2": 8, "do5": 8, "do'": 8, "doh": 8,
}
_NOTE_NAMES = "do re mi fa sol la si do'".split()


def note_name(note: int) -> str:
    """1 → ``do``……8 → ``do'``、0 → ``空拍``（给提示文案用）。"""
    if note == REST:
        return "空拍"
    if MIN_NOTE < note <= MAX_NOTE:
        return _NOTE_NAMES[note - 1]
    return str(note)


def note_of(stem: str) -> int | None:
    """文件名（不含后缀）→ 音符编号；认不出返回 ``None``。

    认这几种：``1``、``01``、``1_do``、``1-do``、``do``、``do2``（高音）、``sol``。
    只看**开头的数字**与**整串唱名**，别的（如 ``take1``）不会误判。
    """
    text = stem.strip().lower()
    head = re.match(r"^(\d{1,2})", text)
    if head:
        value = int(head.group(1))
        return value if MIN_NOTE <= value <= MAX_NOTE else None
    # 唱名：长的先试（do2 优先于 do）
    for alias in sorted(ALIASES, key=len, reverse=True):
        if text == alias or text.startswith(alias + "_") or text.startswith(alias + "-"):
            return ALIASES[alias]
    return None


def audio_paths() -> dict[int, Path]:
    """扫一遍音效目录，返回 ``音符 -> 文件``。同一个音有多个文件时，名字里带数字的那个优先。"""
    found: dict[int, Path] = {}
    directory = paths.AUDIO_DIR
    if not directory.is_dir():
        return found
    for entry in sorted(directory.iterdir()):
        if not entry.is_file() or entry.suffix.lower() not in SUFFIXES:
            continue
        note = note_of(entry.stem)
        if note is None:
            continue
        current = found.get(note)
        # 已经有一个就比"谁的名字里带数字"：1.mp3 比 do.mp3 更明确
        if current is None or (re.match(r"^\d", entry.stem) and not re.match(r"^\d", current.stem)):
            found[note] = entry
    return found


def interval_bounds() -> tuple[int, int]:
    """播放速度的允许范围（毫秒）。"""
    return INTERVAL_RANGE


def default_interval_ms() -> int:
    """设置里的默认速度（毫秒），越界夹回 :data:`INTERVAL_RANGE`。"""
    from app.config import load_settings          # 延迟导入：config 与本模块互相被 import

    config = (load_settings().get("skills") or {}).get("play") or {}
    try:
        value = int(config.get("interval_ms", DEFAULT_INTERVAL_MS))
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_MS
    return max(INTERVAL_RANGE[0], min(INTERVAL_RANGE[1], value))


def speed_of(value: Any) -> int:
    """这一轮用什么速度（毫秒/拍）。

    速度是**模型自己的参数**：抒情就慢、欢快就快；没给（或给了个说不通的值）才落回
    设置里的默认值；超出范围一律夹回来，免得一个 20 毫秒把旋律糊成一团。
    """
    if value is None or value == "":
        return default_interval_ms()
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return default_interval_ms()
    return max(INTERVAL_RANGE[0], min(INTERVAL_RANGE[1], interval))


def clean_notes(value: Any) -> list[int]:
    """把模型给的数组整理成合法拍列表：丢掉非整数与越界值（0 是空拍，保留），截断到上限。"""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        return []
    notes: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if MIN_NOTE <= number <= MAX_NOTE:
            notes.append(number)
    return notes[:MAX_NOTES]


class PlaySkill:
    """演奏一段旋律：``notes`` 是 1~8 的音符数组。"""

    name = "play"
    label = "演奏"
    description = (
        "演奏一段旋律。notes 是音符序列，用简谱数字：1=do、2=re、3=mi、4=fa、5=sol、6=la、"
        "7=si、8=高音 do、0=空拍。只在用户明确要求演奏或同意时调用，不要自己主动演奏；"
        f"按旋律的音高走向编一串音符（3个以上，最多 {MAX_NOTES} 个），并用 0 做停顿换气。"
        "播放速度由你定：用 interval_ms 给每拍的毫秒数。"
        "这一轮不用说话：这个工具会一直等到曲子放完才把结果交回给你，那时你再回应。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "items": {"type": "integer", "minimum": MIN_NOTE, "maximum": MAX_NOTE},
                "description": (
                    "音符序列，每个是 0~8：0=空拍（这一拍什么都不播，只占时间，用来做停顿与"
                    "呼吸）、1=do、2=re、3=mi、4=fa、5=sol、6=la、7=si、8=高音 do。"
                    "例如：1 1 5 5 6 6 5"
                ),
            },
            "interval_ms": {
                "type": "integer",
                "minimum": INTERVAL_RANGE[0],
                "maximum": INTERVAL_RANGE[1],
                "description": (
                    "播放速度：每拍之间多少毫秒。欢快 250~350、正常 450~600、抒情 700~1000；"
                    "留空就用设置里的默认值"
                ),
            },
        },
        "required": ["notes"],
    }

    def __init__(self, player: Callable[[list[int], int], str] | None = None) -> None:
        #: 界面给的播放回调：``(音符列表, 间隔毫秒) -> 一句说明``。
        #: **它会被请求线程调用**，所以实现里只发 Qt 信号、不碰界面（见 PetWindow）。
        self._player = player

    def run(self, arguments: dict[str, Any]) -> str:
        notes = clean_notes(arguments.get("notes"))
        if not notes:
            return (
                f"音符不合法：notes 要给出 {MIN_NOTE}~{MAX_NOTE} 的整数数组"
                "（0=空拍、1=do … 8=高音 do），例如 1 1 5 5 6 6 5。"
            )
        available = audio_paths()
        if not available:
            return (
                f"现在演奏不了：{paths.AUDIO_DIR} 里还没有音效文件。"
            )
        playable = [note for note in notes if note != REST and note in available]
        missing = sorted({note for note in notes if note != REST and note not in available})
        rests = sum(1 for note in notes if note == REST)
        if not playable:
            if rests:
                return "整段都是空拍，没什么可演奏的：notes 里至少要有一个 1~8 的音。"
            return f"这些音都没有对应音效：{missing}（现成的有 {sorted(available)}），换些音再试。"
        if self._player is None:
            return "现在演奏不了：没有可用的播放通道。"
        interval = speed_of(arguments.get("interval_ms"))
        # 缺音效的音就地变成空拍：节拍不断，只是那一拍没出声
        beats = [REST if note == REST or note not in available else note for note in notes]
        devtools.log.info("技能 · 演奏 %s（%d 拍、间隔 %dms）", beats, len(beats), interval)
        # 这行会等曲子放完才回来（见 PetWindow._request_play），所以它带着"演奏完了"的结论
        detail = self._player(beats, interval)
        note = f"；{missing} 没有音效文件，已经换成空拍" if missing else ""
        return (
            f"开始演奏 {len(beats)} 拍（{len(playable)} 个音、{rests} 个空拍{note}），"
            f"每拍间隔 {interval} 毫秒。{detail}"
        )
