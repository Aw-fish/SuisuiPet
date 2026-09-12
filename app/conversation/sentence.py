"""流式文本的句级切分。

对话服务每收到一段增量就喂进来，凑齐整句后通过 ``sentence`` 信号抛出。
P0 阶段暂时没人消费，但语音 TTS 要靠它做到「边生成边播」，
所以先把管线位置留出来。
"""

from __future__ import annotations

SENTENCE_ENDINGS = "。！？!?；;…\n"
SOFT_BREAKS = "，,、:："
#: 单句过长时在标点处强制断开，避免 TTS 要等一整段
MAX_SENTENCE = 60


class SentenceSplitter:
    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, delta: str) -> list[str]:
        """追加增量，返回本次凑齐的完整句子列表。"""
        self._buffer += delta
        sentences: list[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            chunk = self._buffer[:cut]
            self._buffer = self._buffer[cut:]
            sentence = chunk.strip()
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self) -> str:
        """流结束时吐出残余内容。"""
        sentence = self._buffer.strip()
        self._buffer = ""
        return sentence

    def reset(self) -> None:
        self._buffer = ""

    def _find_cut(self) -> int | None:
        for index, char in enumerate(self._buffer):
            if char in SENTENCE_ENDINGS:
                return index + 1
        if len(self._buffer) < MAX_SENTENCE:
            return None
        for index in range(MAX_SENTENCE - 1, 0, -1):
            if self._buffer[index] in SOFT_BREAKS:
                return index + 1
        return MAX_SENTENCE
