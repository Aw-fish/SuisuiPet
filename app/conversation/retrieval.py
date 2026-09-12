"""记忆检索：自建二元组切分 + SQLite FTS5，BM25 排序。

**为什么不用 FTS5 自带的 trigram 分词器**

实测（SQLite 3.37）trigram 对少于 3 个字符的查询会**静默返回空结果**：
``MATCH '上海'`` 与 ``LIKE '%上海%'`` 都是 0 条，而中文里二字词恰恰最常见。

**这里的做法**

把正文预先切成字符二元组、用空格连接后入库，交给 FTS5 默认分词器（unicode61）
按空格切词。查询词也切成二元组，用**短语查询**保证这些二元组在原文里是连续出现的：

    原文  用户下周要去上海出差三天
    入库  用户 户下 下周 周要 要去 去上 上海 海出 出差
    查询  "上海 海出 出差"      → 只有"上海出差"连续出现的那条能命中

这样二字、三字、四字查询都能匹配，拿到的是真正的 BM25 排序，而且不依赖 trigram
（SQLite 3.34 以下同样可用）。代价是索引膨胀约 2.6 倍、单字查询匹配不到。

``memory.db`` 只是索引，随时可以由 ``memories.jsonl`` 重建，损坏了删掉即可。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: 建索引 / 切词时忽略的空白字符
_BLANK = " \t\r\n\u3000\ufeff"

#: 单次查询里最多拼多少个检索词，防止极端输入把 SQL 撑爆
MAX_TERMS = 48

#: FTS5 短语里需要转义的字符
_UNSAFE = '"'


def bigrams(text: str) -> list[str]:
    """切成字符二元组：``'上海出差'`` -> ``['上海', '海出', '出差']``。"""
    body = "".join(ch for ch in text if ch not in _BLANK)
    if len(body) < 2:
        return [body] if body else []
    return [body[i : i + 2] for i in range(len(body) - 1)]


def index_body(text: str) -> str:
    """入库用的正文：二元组用空格连起来。"""
    return " ".join(bigrams(text))


def _phrase(term: str) -> str:
    """把检索词转成 FTS5 短语：二元组按顺序连写，整体加引号。"""
    grams = [gram.replace(_UNSAFE, "") for gram in bigrams(term)]
    grams = [gram for gram in grams if gram]
    return '"' + " ".join(grams) + '"'


def query_terms(text: str) -> list[str]:
    """从用户输入里抽取检索词。

    中文没有分词器，这里用**滑动窗口取 2~4 字的片段**作为候选词：
    比逐字相交更能保住"连续出现"这个约束，也不会像整句短语那样几乎必然落空。
    """
    body = "".join(ch if ch not in _BLANK else " " for ch in text)
    terms: dict[str, None] = {}
    for chunk in body.split():
        # 去掉标点，保留中英文与数字
        chunk = "".join(ch for ch in chunk if ch.isalnum())
        for size in (2, 3, 4):
            for start in range(0, max(0, len(chunk) - size + 1)):
                piece = chunk[start : start + size]
                if len(piece) == size:
                    terms.setdefault(piece, None)
    return list(terms)[:MAX_TERMS]


class MemoryIndex:
    """``memories.jsonl`` 的 FTS5 索引。索引与数据分离，可随时重建。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ---- 内部 ---------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memories USING fts5(body, entry_id UNINDEXED)"
        )
        return connection

    @staticmethod
    def available() -> bool:
        """当前 Python 内置的 SQLite 是否支持 FTS5。"""
        try:
            with sqlite3.connect(":memory:") as probe:
                probe.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
            return True
        except sqlite3.Error:
            return False

    # ---- 对外 ---------------------------------------------------------------

    def rebuild(self, entries) -> bool:
        """按当前记忆条目全量重建索引。返回是否成功。"""
        try:
            connection = self._connect()
        except sqlite3.Error:
            return False
        try:
            connection.execute("DELETE FROM memories")
            connection.executemany(
                "INSERT INTO memories(body, entry_id) VALUES (?, ?)",
                [(index_body(entry.text), entry.id) for entry in entries],
            )
            connection.commit()
            return True
        except sqlite3.Error:
            return False
        finally:
            connection.close()

    def search(self, terms: list[str], limit: int = 60) -> dict[str, float]:
        """把检索词用 OR 拼成**一次**查询，返回 ``{entry_id: 相关性}``。

        用一次查询而不是逐词查询：既省掉重复扫描，也让 BM25 的 IDF 在同一份
        查询里统一计算（一个文档命中越多词、命中越罕见的词，得分越高）。
        """
        scores: dict[str, float] = {}
        phrases = [_phrase(term) for term in terms if len(term.strip()) >= 2]
        phrases = [p for p in phrases if len(p) > 2]
        if not phrases:
            return scores
        try:
            connection = self._connect()
        except sqlite3.Error:
            return scores
        try:
            statement = (
                "SELECT entry_id, bm25(memories) AS rank FROM memories "
                f"WHERE memories MATCH ? ORDER BY rank LIMIT {int(limit)}"
            )
            for entry_id, rank in connection.execute(statement, (" OR ".join(phrases),)):
                # bm25() 返回负数，越小越相关；取相反数变成"越大越好"
                scores[str(entry_id)] = scores.get(str(entry_id), 0.0) - float(rank)
        except sqlite3.Error:
            return {}
        finally:
            connection.close()
        return scores

    def matched_terms(self, terms: list[str], entry_id: str) -> list[str]:
        """回查某条记忆命中了哪些词，用于在界面上解释召回原因。"""
        hits: list[str] = []
        try:
            connection = self._connect()
        except sqlite3.Error:
            return hits
        try:
            row = connection.execute(
                "SELECT body FROM memories WHERE entry_id = ?", (str(entry_id),)
            ).fetchone()
        except sqlite3.Error:
            return hits
        finally:
            connection.close()
        if not row:
            return hits
        body = str(row[0])
        for term in terms:
            grams = bigrams(term)
            if grams and all(gram in body for gram in grams):
                hits.append(term)
        return hits
