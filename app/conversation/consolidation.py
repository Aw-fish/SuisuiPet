"""记忆整理：把一段会话提炼成记忆条目 + 滚动摘要。

一次整理 = 一次 LLM 调用（抽取事实 / 偏好 / 事件 / 约定，并合并摘要）→
去重合并进 ``memories.jsonl`` → 重写滚动摘要 → 从 ``pending.json`` 里移除该会话。

幂等性由 ``pending.json`` 保证：只有仍在清单里的会话才会被整理；即使重复整理，
``merge()`` 也会按文本相似度去重，不会产生重复记忆。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.conversation import clock
from app.conversation.memory import (
    KINDS,
    KIND_FACT,
    MemoryEntry,
    MemoryStore,
    _parse,
    _stamp,
)
from app.conversation.message import ROLE_ASSISTANT, ROLE_USER
from app.conversation.providers.base import LLMProvider, ProviderError

#: 参与整理的最近消息条数上限（更早的交给滚动摘要）
MAX_TRANSCRIPT_MESSAGES = 40
#: 滚动摘要的目标长度，防止无限增长
MAX_SUMMARY_CHARS = 300

_SYSTEM = (
    "你在分析一段用户与桌面宠物的对话，负责提炼值得长期记住的信息。"
    "只输出一个 JSON 对象，不要任何解释、前后缀或 Markdown 代码块。"
)

_USER_TEMPLATE = """请从下面的对话里提炼值得长期记住的信息。

这段对话发生在 {when}。

严格输出这个 JSON 结构：
{{
  "memories": [
    {{"kind": "fact|preference|event|promise", "text": "一句话陈述", "importance": 3}}
  ],
  "summary": "合并后的滚动摘要"
}}

规则：
1. kind 只能取：fact（客观事实）、preference（用户的喜好或习惯）、event（这次发生的事）、promise（用户提到的约定或待办）
2. text 用第三人称、以"用户"开头陈述，例如"用户住在杭州"，一句话一条，不要多条挤在一起
3. importance 取 1~5：1 无关紧要，5 非常重要
4. 寒暄、道别、无信息量的内容不要记；没有值得记的就输出空数组
5. summary：把下面「已有的摘要」和这次对话的新内容**合并**成一段不超过 {limit} 字的中文摘要，去掉已经无关紧要的旧内容；没有内容就留空字符串
6. text 里不要出现"今天""明天""下周"这类会过期的说法：结合上面给出的日期换算成绝对日期再写，
   例如"用户将于 2026-09-21 那一周去上海出差"

已有的摘要（可能为空）：
<previous>
{previous}
</previous>

对话：
{transcript}
"""


def session_when(messages) -> str:
    """这段对话发生在什么时候，给整理提示词当日期锚点。

    模型做日期算术并不可靠，所以单日会话连星期一起给出，方便把"周五之前"
    这类说法落到具体日子上。没有这个锚点，"下周要去上海"会被原样存下来，
    过两周就成了一条悬空的记忆。
    """
    stamps = [
        moment for moment in (_parse(message.ts) for message in messages) if moment is not None
    ]
    if not stamps:
        return ""
    first, last = min(stamps), max(stamps)
    if first.date() == last.date():
        return f"{first:%Y-%m-%d}（{clock.WEEKDAYS[first.weekday()]}）"
    return f"{first:%Y-%m-%d} 至 {last:%Y-%m-%d}"


def build_transcript(messages, limit: int = MAX_TRANSCRIPT_MESSAGES) -> str:
    lines: list[str] = []
    for message in messages[-limit:]:
        text = message.content.strip()
        if not text:
            continue
        speaker = "用户" if message.role == ROLE_USER else "助手"
        lines.append(f"{speaker}：{text}")
    return "\n".join(lines)


def _parse_json(text: str) -> dict:
    """从模型输出里剥出 JSON：容忍前后缀与代码块围栏。"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_importance(value) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 3


def extract(
    provider: LLMProvider, transcript: str, previous_summary: str = "", when: str = ""
) -> tuple[list[dict], str]:
    """一次调用，返回 (记忆字典列表, 合并后的摘要)。"""
    prompt = _USER_TEMPLATE.format(
        limit=MAX_SUMMARY_CHARS,
        when=when or "（时间未知）",
        previous=previous_summary or "（无）",
        transcript=transcript,
    )
    raw = provider.complete(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        max_tokens=900,
        timeout=60,
        temperature=0,
    )
    payload = _parse_json(raw)
    raw_memories = payload.get("memories")
    memories = raw_memories if isinstance(raw_memories, list) else []
    summary = str(payload.get("summary") or "").strip()
    return memories, summary


def consolidate_session(
    store: MemoryStore,
    provider: LLMProvider,
    session_path: Path,
    messages: list,
) -> tuple[int, int]:
    """整理一段会话，返回 (新增数, 合并数)；失败返回 (-1, 0) 并保留 pending。"""
    transcript = build_transcript(messages)
    name = session_path.name
    if not transcript:
        # 没有实质内容的会话直接清掉清单，不浪费一次调用
        store.clear_pending(name)
        return 0, 0
    try:
        raw_memories, summary = extract(
            provider, transcript, store.summary_text(), session_when(messages)
        )
    except ProviderError:
        # 网络失败：保留 pending，下次启动再补做
        return -1, 0
    new_entries: list[MemoryEntry] = []
    for item in raw_memories:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", KIND_FACT))
        if kind not in KINDS:
            kind = KIND_FACT
        text = " ".join(str(item.get("text", "")).split())
        if not text:
            continue
        new_entries.append(
            MemoryEntry(
                id="",
                text=text,
                kind=kind,
                importance=_coerce_importance(item.get("importance")),
                ts=_stamp(),
                source=name,
            )
        )
    ids = store.next_id(len(new_entries))
    for entry, entry_id in zip(new_entries, ids):
        entry.id = entry_id
    added, merged = store.merge(new_entries)
    if summary:
        store.write_summary(summary)
    store.clear_pending(name)
    store.rebuild_index()
    return added, merged


def consolidate_pending(store: MemoryStore, provider: LLMProvider) -> tuple[int, int]:
    """整理所有待处理的会话，返回 (新增数, 合并数)。

    过短的会话（不足 2 条非空消息）没有可提炼的内容，直接清掉清单，不浪费调用。
    """
    total_added = total_merged = 0
    for name in store.pending():
        session_path = store.sessions_dir / name
        messages = [m for m in store.read_session(session_path) if m.content.strip()]
        if len(messages) < 2:
            store.clear_pending(name)
            continue
        added, merged = consolidate_session(store, provider, session_path, messages)
        if added < 0:
            break
        total_added += added
        total_merged += merged
    return total_added, total_merged
