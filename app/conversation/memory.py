"""角色记忆，三层分开存放。

* **L1 工作上下文**：每次请求发给模型的消息列表，由 :meth:`build_context` 组装。
* **L2 会话日志**：逐条原始消息追加到 ``memory/sessions/<时间戳>.jsonl``，崩溃安全。
* **L3 长期记忆**：``memory/memories.jsonl`` 存提炼后的记忆条目（事实 / 偏好 /
  事件 / 约定），每条带时间、重要度与动态权重；``memory/summaries/<年月>.md``
  存滚动摘要。

全部落在角色目录下（``data/characters/<角色名>/memory/``），换角色即换记忆。

``memories.jsonl`` 是长期记忆的**唯一事实来源**：``memory.db`` 只是它的 FTS5
索引，删掉可以重建；"稳定事实"也不再单独放一个 profile.json，而是 ``kind`` 为
``fact`` / ``preference`` 的条目——同一件事只有一个写入口，不会两处打架。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app import characters
from app.conversation.message import (
    Message,
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_USER,
)
from app.conversation.retrieval import MAX_TERMS, MemoryIndex, query_terms

SESSIONS_DIRNAME = "sessions"
SUMMARIES_DIRNAME = "summaries"
MEMORY_NAME = "memories.jsonl"
ARCHIVE_NAME = "archive.jsonl"
PENDING_NAME = "pending.json"
ALIASES_NAME = "aliases.json"
INDEX_NAME = "memory.db"
#: 旧版把摘要写成单个 summary.md，仍然读取以兼容
LEGACY_SUMMARY_NAME = "summary.md"

MAX_HISTORY = 200

#: 记忆种类，对应认知科学里的语义 / 程序性 / 情节 / 前瞻记忆
KIND_FACT = "fact"
KIND_PREFERENCE = "preference"
KIND_EVENT = "event"
KIND_PROMISE = "promise"
KINDS = (KIND_FACT, KIND_PREFERENCE, KIND_EVENT, KIND_PROMISE)

KIND_LABELS = {
    KIND_FACT: "事实",
    KIND_PREFERENCE: "偏好",
    KIND_EVENT: "事件",
    KIND_PROMISE: "约定",
}

#: 权重的半衰期（天）：越久没被用到，召回分越低
HALF_LIFE_DAYS = 45.0
#: 有效权重低于这个值就归档（移出检索池，但不删除）
ARCHIVE_THRESHOLD = 0.06
#: 重要度达到这个级别的记忆不参与自动归档
KEEP_IMPORTANCE = 4
#: 每次注入 system 的记忆条数上限
RECALL_LIMIT = 5
#: 召回分的绝对下限，低于它的直接丢弃（宁可少给，不要给错）
RECALL_FLOOR = 0.2
#: 判定"同一条记忆"的文本相似度阈值（字符二元组 Jaccard）
DEDUPE_RATIO = 0.62


def _now() -> datetime:
    return datetime.now()


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds")


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def similarity(left: str, right: str) -> float:
    """字符二元组的 Jaccard 相似度，用来识别"换了个说法"的重复记忆。"""
    a = {left[i : i + 2] for i in range(max(0, len(left) - 1))} or {left}
    b = {right[i : i + 2] for i in range(max(0, len(right) - 1))} or {right}
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class MemoryEntry:
    """一条长期记忆。"""

    id: str
    text: str
    kind: str = KIND_FACT
    ts: str = ""
    importance: int = 3
    weight: float = 1.0
    hits: int = 0
    last_used: str = ""
    aliases: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def importance_factor(self) -> float:
        """重要度换算成 0.6 ~ 1.0 的系数，避免它完全盖过相关性。"""
        level = max(1, min(5, int(self.importance)))
        return 0.6 + 0.4 * (level - 1) / 4

    def freshness(self, now: datetime | None = None) -> float:
        """半衰期衰减，返回 0~1 的新鲜度。"""
        anchor = _parse(self.last_used) or _parse(self.ts)
        if anchor is None:
            return 1.0
        days = max(0.0, ((now or _now()) - anchor).total_seconds() / 86400)
        return 0.5 ** (days / HALF_LIFE_DAYS)

    def score_base(self, now: datetime | None = None) -> float:
        """不含相关性的基础分（重要度 × 新鲜度）。"""
        return self.importance_factor * max(self.freshness(now), 0.15)

    def to_record(self) -> dict:
        record: dict = {
            "id": self.id,
            "ts": self.ts,
            "kind": self.kind,
            "text": self.text,
            "importance": self.importance,
            "weight": round(self.weight, 4),
            "hits": self.hits,
        }
        if self.last_used:
            record["last_used"] = self.last_used
        if self.aliases:
            record["aliases"] = list(self.aliases)
        if self.source:
            record["source"] = self.source
        return record

    @classmethod
    def from_record(cls, record: dict) -> "MemoryEntry":
        kind = str(record.get("kind", KIND_FACT))
        if kind not in KINDS:
            kind = KIND_FACT
        try:
            importance = int(record.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        try:
            weight = float(record.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        raw_aliases = record.get("aliases")
        return cls(
            id=str(record.get("id", "")),
            text=" ".join(str(record.get("text", "")).split()),
            kind=kind,
            ts=str(record.get("ts", "")),
            importance=max(1, min(5, importance)),
            weight=max(0.0, min(1.0, weight)),
            hits=max(0, int(record.get("hits", 0) or 0)),
            last_used=str(record.get("last_used", "")),
            aliases=[str(item) for item in raw_aliases] if isinstance(raw_aliases, list) else [],
            source=str(record.get("source", "")),
        )


@dataclass
class RecallHit:
    """一条召回结果，带召回分与命中词（用于向用户解释为什么想起这条）。"""

    entry: MemoryEntry
    score: float
    matched: list[str] = field(default_factory=list)


class MemoryStore:
    def __init__(self, character: str) -> None:
        self.character = character
        self.root = characters.memory_dir(character)
        self.sessions_dir = self.root / SESSIONS_DIRNAME
        self.session_path = self._latest_session() or self._new_session_path()

    # ---- 路径 ---------------------------------------------------------------

    @property
    def summaries_dir(self) -> Path:
        return self.root / SUMMARIES_DIRNAME

    @property
    def entries_path(self) -> Path:
        return self.root / MEMORY_NAME

    @property
    def archive_path(self) -> Path:
        return self.root / ARCHIVE_NAME

    @property
    def pending_path(self) -> Path:
        return self.root / PENDING_NAME

    @property
    def aliases_path(self) -> Path:
        return self.root / ALIASES_NAME

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_NAME

    @property
    def legacy_summary_path(self) -> Path:
        return self.root / LEGACY_SUMMARY_NAME

    # ---- L2 会话日志 ---------------------------------------------------------

    def sessions(self) -> list[Path]:
        """磁盘上的会话文件，按时间排序（最新的在最后）。"""
        if not self.sessions_dir.is_dir():
            return []
        return sorted(self.sessions_dir.glob("*.jsonl"))

    def _latest_session(self) -> Path | None:
        found = self.sessions()
        return found[-1] if found else None

    def _new_session_path(self) -> Path:
        stamp = _now().strftime("%Y-%m-%d_%H%M%S")
        path = self.sessions_dir / f"{stamp}.jsonl"
        index = 2
        while path.exists():
            path = self.sessions_dir / f"{stamp}-{index}.jsonl"
            index += 1
        return path

    def new_session(self) -> Path:
        """切到一段新会话（只换路径，不落盘；第一条消息写入时才建文件）。"""
        self.session_path = self._new_session_path()
        return self.session_path

    def append(self, message: Message) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        with self.session_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message.to_record(), ensure_ascii=False) + "\n")

    def read_session(self, path: Path) -> list[Message]:
        messages: list[Message] = []
        if not path.is_file():
            return messages
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                messages.append(Message.from_record(record))
        return messages

    def load(self, limit: int = MAX_HISTORY) -> list[Message]:
        return self.read_session(self.session_path)[-limit:]

    # ---- 待整理清单（整理流程的幂等依据）--------------------------------------

    def pending(self) -> list[str]:
        """尚未整理的会话文件名。"""
        if not self.pending_path.is_file():
            return []
        try:
            data = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [str(item) for item in data]

    def _write_pending(self, names: list[str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not names:
            self.pending_path.unlink(missing_ok=True)
            return
        self.pending_path.write_text(
            json.dumps(sorted(set(names)), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def mark_pending(self, name: str) -> None:
        names = self.pending()
        if name not in names:
            names.append(name)
            self._write_pending(names)

    def clear_pending(self, name: str) -> None:
        names = self.pending()
        if name in names:
            names.remove(name)
            self._write_pending(names)

    def unconsolidated(self) -> list[tuple[Path, list[Message]]]:
        """还没整理、且确实有内容的会话（(文件, 消息列表)）。"""
        result: list[tuple[Path, list[Message]]] = []
        for name in self.pending():
            path = self.sessions_dir / name
            messages = [m for m in self.read_session(path) if m.content.strip()]
            if len(messages) >= 2:
                result.append((path, messages))
        return result

    # ---- L3 记忆条目 ---------------------------------------------------------

    def entries(self, include_archived: bool = False) -> list[MemoryEntry]:
        found: list[MemoryEntry] = []
        for path in (self.entries_path, self.archive_path) if include_archived else (self.entries_path,):
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    entry = MemoryEntry.from_record(record)
                    if entry.id and entry.text:
                        found.append(entry)
        return found

    def _write_entries(self, entries: list[MemoryEntry]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(entry.to_record(), ensure_ascii=False) + "\n" for entry in entries
        )
        self.entries_path.write_text(payload, encoding="utf-8")

    def next_id(self, count: int = 1) -> list[str]:
        """按"日期_序号"生成稳定的记忆 id。"""
        prefix = _now().strftime("m_%Y%m%d_%H%M%S")
        existing = {entry.id for entry in self.entries(include_archived=True)}
        ids: list[str] = []
        index = 1
        while len(ids) < count:
            candidate = f"{prefix}_{index:02d}"
            if candidate not in existing:
                ids.append(candidate)
            index += 1
        return ids

    @staticmethod
    def _find_duplicate(entries: list[MemoryEntry], text: str) -> MemoryEntry | None:
        for existing in entries:
            if existing.text == text:
                return existing
            if similarity(existing.text, text) >= DEDUPE_RATIO:
                return existing
        return None

    def merge(self, incoming: list[MemoryEntry]) -> tuple[int, int]:
        """把新抽取的记忆并进库，同一条只保留一份。返回 (新增数, 合并数)。"""
        entries = self.entries()
        added = merged = 0
        for entry in incoming:
            text = " ".join(entry.text.split())
            if not text:
                continue
            entry.text = text
            duplicate = self._find_duplicate(entries, text)
            if duplicate is None:
                entry.ts = entry.ts or _stamp()
                entry.weight = 1.0
                entries.append(entry)
                added += 1
                continue
            # 又被提起 = 这条记忆还活着：权重回升、刷新使用时间
            if len(text) > len(duplicate.text):
                duplicate.text = text
            if entry.kind:
                duplicate.kind = entry.kind
            duplicate.importance = max(duplicate.importance, entry.importance)
            duplicate.weight = min(1.0, duplicate.weight + 0.15)
            duplicate.last_used = _stamp()
            duplicate.aliases = sorted(set(duplicate.aliases) | set(entry.aliases))
            duplicate.source = entry.source or duplicate.source
            merged += 1
        self._write_entries(entries)
        return added, merged

    def touch(self, ids: list[str]) -> None:
        """被召回的条目做一次强化：次数 +1、权重回升。"""
        wanted = {str(item) for item in ids}
        if not wanted:
            return
        entries = self.entries()
        stamp = _stamp()
        changed = False
        for entry in entries:
            if entry.id in wanted:
                entry.hits += 1
                entry.weight = min(1.0, entry.weight + 0.2)
                entry.last_used = stamp
                changed = True
        if changed:
            self._write_entries(entries)

    def forget(self, ids: list[str]) -> int:
        """删除指定记忆（用户在记忆管理界面里的操作）。"""
        wanted = {str(item) for item in ids}
        entries = self.entries()
        kept = [entry for entry in entries if entry.id not in wanted]
        removed = len(entries) - len(kept)
        if removed:
            self._write_entries(kept)
            self.rebuild_index()
        return removed

    def update(self, entry_id: str, text: str, kind: str, importance: int) -> bool:
        """修改一条记忆的内容与属性。"""
        entries = self.entries()
        for entry in entries:
            if entry.id == entry_id:
                entry.text = " ".join(text.split()) or entry.text
                entry.kind = kind if kind in KINDS else entry.kind
                entry.importance = max(1, min(5, int(importance)))
                self._write_entries(entries)
                self.rebuild_index()
                return True
        return False

    def decay(self, now: datetime | None = None) -> int:
        """把有效权重过低的记忆移进归档文件，返回归档条数。"""
        moment = now or _now()
        entries = self.entries()
        kept: list[MemoryEntry] = []
        dropped: list[MemoryEntry] = []
        for entry in entries:
            expired = entry.weight * entry.freshness(moment) < ARCHIVE_THRESHOLD
            if expired and entry.importance < KEEP_IMPORTANCE:
                dropped.append(entry)
            else:
                kept.append(entry)
        if dropped:
            self._write_entries(kept)
            self.root.mkdir(parents=True, exist_ok=True)
            with self.archive_path.open("a", encoding="utf-8") as handle:
                for entry in dropped:
                    handle.write(json.dumps(entry.to_record(), ensure_ascii=False) + "\n")
            self.rebuild_index()
        return len(dropped)

    def profile_text(self) -> str:
        """稳定事实与偏好：量小且总是相关，全量注入 system。"""
        lines = [
            f"- {entry.text}"
            for entry in self.entries()
            if entry.kind in (KIND_FACT, KIND_PREFERENCE)
        ]
        return "\n".join(lines)

    # ---- L3 摘要 -------------------------------------------------------------

    def write_summary(self, text: str, month: str | None = None) -> None:
        """按月份写入滚动摘要。"""
        self.summaries_dir.mkdir(parents=True, exist_ok=True)
        name = month or _now().strftime("%Y-%m")
        (self.summaries_dir / f"{name}.md").write_text(text.strip() + "\n", encoding="utf-8")

    def summary_text(self, months: int = 3) -> str:
        """把最近几个月的摘要拼起来；顺带兼容旧的单文件 summary.md。"""
        chunks: list[str] = []
        if self.legacy_summary_path.is_file():
            try:
                chunks.append(self.legacy_summary_path.read_text(encoding="utf-8").strip())
            except OSError:
                pass
        if self.summaries_dir.is_dir():
            files = sorted(self.summaries_dir.glob("*.md"))[-months:]
            for path in files:
                try:
                    text = path.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if text:
                    chunks.append(text)
        return "\n\n".join(chunk for chunk in chunks if chunk)

    # ---- 别名表 -------------------------------------------------------------

    def aliases(self) -> dict[str, list[str]]:
        if not self.aliases_path.is_file():
            return {}
        try:
            data = json.loads(self.aliases_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        result: dict[str, list[str]] = {}
        for key, value in data.items():
            if isinstance(value, list):
                result[str(key)] = [str(item) for item in value]
        return result

    def _expand_terms(self, terms: list[str]) -> list[str]:
        """用别名表补上同义表达，缓解"换个说法就找不到"。"""
        alias_map = self.aliases()
        if not alias_map:
            return terms
        expanded = list(terms)
        seen = set(terms)
        for term in terms:
            for canonical, variants in alias_map.items():
                group = [canonical, *variants]
                if not any(term == item or term in item or item in term for item in group):
                    continue
                for item in group:
                    if item and item not in seen:
                        seen.add(item)
                        expanded.append(item)
        return expanded[: MAX_TERMS * 2]

    # ---- 检索 ---------------------------------------------------------------

    def _index(self) -> MemoryIndex:
        return MemoryIndex(self.index_path)

    def rebuild_index(self) -> bool:
        return self._index().rebuild(self.entries())

    def recall(self, query: str, k: int = RECALL_LIMIT) -> list[RecallHit]:
        """召回与当前话题相关的记忆。

        排序分 = 相关性 × 重要度 × 新鲜度；相关性命中得越准越高，重要度让它
        在同等相关时优先，新鲜度让久未提到的记忆自然下沉。
        """
        entries = self.entries()
        if not entries or not query.strip():
            return []
        terms = self._expand_terms(query_terms(query))
        if not terms:
            return []
        scores = self._index().search(terms)
        candidates: list[tuple[float, MemoryEntry, list[str]]] = []
        if scores:
            top = max(scores.values()) or 1.0
            for entry in entries:
                raw = scores.get(entry.id)
                if raw is None:
                    continue
                relevance = raw / top
                candidates.append((relevance * entry.score_base(), entry, []))
        if not candidates:
            # 索引不可用（或检索词太短）时退化为子串匹配，保证功能不失效
            for entry in entries:
                hits = [term for term in terms if term in entry.text]
                if not hits:
                    continue
                relevance = len(hits) / max(1, len(terms))
                candidates.append((relevance * entry.score_base(), entry, hits))
        candidates.sort(key=lambda item: item[0], reverse=True)
        results = [
            RecallHit(entry, score, matched)
            for score, entry, matched in candidates
            if score >= RECALL_FLOOR
        ][:k]
        return results

    # ---- L1 上下文组装 -------------------------------------------------------

    def build_context(self, system_prompt: str, limit: int, query: str = "") -> list[dict]:
        sections: list[str] = []
        if system_prompt.strip():
            sections.append(system_prompt.strip())
        profile = self.profile_text()
        if profile:
            sections.append(f"关于用户的长期记忆：\n{profile}")
        hits = self.recall(query, RECALL_LIMIT) if query.strip() else []
        if hits:
            lines = [f"- （{hit.entry.label}）{hit.entry.text}" for hit in hits]
            sections.append("可能与当前话题相关的记忆：\n" + "\n".join(lines))
            # 被注入上下文即视为"用到了一次"，用于强化权重
            self.touch([hit.entry.id for hit in hits])
        summary = self.summary_text()
        if summary:
            sections.append(f"更早的对话摘要：\n{summary}")
        context: list[dict] = []
        if sections:
            context.append({"role": ROLE_SYSTEM, "content": "\n\n".join(sections)})
        history = [
            message
            for message in self.load()
            if message.role in (ROLE_USER, ROLE_ASSISTANT) and message.content.strip()
        ]
        for message in history[-max(1, limit):]:
            context.append(message.to_api())
        return context
