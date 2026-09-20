"""情绪标记：让立绘表情跟着回复的语气走。

做法是**让模型在回复里内嵌标记**（``[开心]`` 这类），客户端只负责两件事：
把标记从正文里剥干净，以及按标记切换立绘表情。

流式下标记可能被切在两个分片之间（``[开`` + ``心]``），所以过滤器会把"疑似还没
收尾的标记"先扣住，等后续分片到齐再决定，避免正文里漏出半截方括号。
"""

from __future__ import annotations

#: 模型可用的情绪标记 -> 立绘素材名；``None`` 表示回到默认立绘
MOOD_TAGS: dict[str, str | None] = {
    "开心": "Happy",
    "难过": "Sad",
    "生气": "Angry",
    "惊讶": "Surprised",
    "思考": "Think",
    "平常": None,
}

#: 标记的括号形式（模型偶尔会用全角）
OPEN_BRACKETS = "[【"
CLOSE_BRACKETS = "]】"

#: 扣住这么多字符还没收尾，就当作普通文本放出去，免得正文被误吞
MAX_HOLD = 12

#: 提示词里给模型的说明，只在开启「自动表情」时才会追加
EXPRESSION_HINT = (
    "- 在回复里用方括号标出表情，让它跟着语气走，可选：[开心] [难过] [生气] [惊讶] [思考] [平常]。"
    "一次回复最多标记一次，放在语气发生变化的那句话前面，语气平缓时用 [平常]。"
    "标记不算内容，不要解释它。"
)

# ---- 防抖参数：想调节奏只改这张表 -------------------------------------------
#: 灵敏度(1~10) -> (最短停留秒, 无新情绪后回归默认的分钟数)
#: 5 是推荐值：切换不频繁也不迟钝。
MOOD_TIMING: dict[int, tuple[float, float]] = {
    1: (4.0, 8.0),
    2: (3.5, 7.0),
    3: (3.0, 6.0),
    4: (2.2, 4.5),
    5: (1.5, 3.0),
    6: (1.3, 2.5),
    7: (1.1, 2.0),
    8: (1.0, 1.5),
    9: (0.9, 1.2),
    10: (0.8, 1.0),
}

DEFAULT_SENSITIVITY = 5


def mood_timing(sensitivity: int) -> tuple[int, int]:
    """把灵敏度换算成 ``(最短停留毫秒, 超时回归毫秒)``。"""
    try:
        level = int(sensitivity)
    except (TypeError, ValueError):
        level = DEFAULT_SENSITIVITY
    level = max(min(MOOD_TIMING), min(max(MOOD_TIMING), level))
    dwell_seconds, timeout_minutes = MOOD_TIMING[level]
    return round(dwell_seconds * 1000), round(timeout_minutes * 60_000)


def mood_asset(tag: str) -> str | None:
    """标记名 -> 立绘素材名；``None`` 表示回到默认立绘。"""
    return MOOD_TAGS.get(tag)


class TagFilter:
    """从流式文本里剥掉情绪标记，并把识别到的标记吐出来。

    用法：每收到一段增量就 ``feed()``，得到"可以安全展示的文本"和"这段里带的表情"；
    流结束时再 ``flush()`` 一次，把扣住的残余内容取回。
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._last_char = ""

    def reset(self) -> None:
        self._buffer = ""
        self._last_char = ""

    def feed(self, delta: str) -> tuple[str, list[str]]:
        """返回 ``(干净文本, 识别到的标记列表)``。"""
        self._buffer += delta
        parts: list[str] = []
        tags: list[str] = []
        while self._buffer:
            start = self._find_open(self._buffer)
            if start is None:
                parts.append(self._buffer)
                self._buffer = ""
                break
            if start > 0:
                parts.append(self._buffer[:start])
                self._buffer = self._buffer[start:]
            end = self._find_close(self._buffer)
            if end is None:
                # 还没收尾：先扣住等后续分片；扣太久了就当作普通文本放出去
                if len(self._buffer) > MAX_HOLD:
                    parts.append(self._buffer)
                    self._buffer = ""
                break
            inner = self._buffer[1:end]
            if inner in MOOD_TAGS:
                tags.append(inner)
                self._buffer = self._buffer[end + 1 :]
                self._drop_following_space(parts)
                continue
            # 不是已知标记：原样保留，继续往后找
            parts.append(self._buffer[: end + 1])
            self._buffer = self._buffer[end + 1 :]
        text = "".join(parts)
        if text:
            self._last_char = text[-1]
        return text, tags

    def flush(self) -> tuple[str, list[str]]:
        """流结束：把扣住的内容当普通文本吐出来。"""
        text, self._buffer = self._buffer, ""
        if text:
            self._last_char = text[-1]
        return text, []

    def _drop_following_space(self, parts: list[str]) -> None:
        """吃掉标记后面的一个空格——中文正文里那只是分隔符留下的噪音。

        只在前面不是 ASCII 字母数字时才吃，免得把英文的 ``hello [happy] world``
        粘成 ``helloworld``。
        """
        if self._buffer[:1] != " ":
            return
        previous = parts[-1][-1] if parts and parts[-1] else self._last_char
        if not previous or not previous.isascii():
            self._buffer = self._buffer[1:]

    @staticmethod
    def _find_open(text: str) -> int | None:
        positions = [text.find(char) for char in OPEN_BRACKETS]
        found = [index for index in positions if index >= 0]
        return min(found) if found else None

    @staticmethod
    def _find_close(text: str) -> int | None:
        positions = [text.find(char, 1) for char in CLOSE_BRACKETS]
        found = [index for index in positions if index >= 0]
        return min(found) if found else None
