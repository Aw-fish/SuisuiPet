"""角色记忆，三层分开存放。

* **L1 工作上下文**：每次请求发给模型的消息列表，由 :meth:`build_context` 组装。
* **L2 会话日志**：逐条原始消息追加到 ``memory/sessions/<时间戳>.jsonl``，崩溃安全。
* **L3 长期记忆**：``memory/profile.json`` 存稳定事实与偏好，``memory/summary.md``
  存早期对话摘要，下次渲染进 system prompt。

全部落在角色目录下（``data/characters/<角色名>/memory/``），换角色即换记忆。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app import characters
from app.conversation.message import (
    Message,
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_USER,
)

SESSIONS_DIRNAME = "sessions"
PROFILE_NAME = "profile.json"
SUMMARY_NAME = "summary.md"
MAX_HISTORY = 200


class MemoryStore:
    def __init__(self, character: str) -> None:
        self.character = character
        self.root = characters.memory_dir(character)
        self.sessions_dir = self.root / SESSIONS_DIRNAME
        self.session_path = self._latest_or_new()

    # ---- 路径 ---------------------------------------------------------------

    @property
    def profile_path(self) -> Path:
        return self.root / PROFILE_NAME

    @property
    def summary_path(self) -> Path:
        return self.root / SUMMARY_NAME

    def _latest_or_new(self) -> Path:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions = sorted(self.sessions_dir.glob("*.jsonl"))
        if sessions:
            return sessions[-1]
        return self.new_session()

    def new_session(self) -> Path:
        """开一段新会话，旧的 jsonl 文件保留。"""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = self.sessions_dir / f"{stamp}.jsonl"
        if not path.exists():
            path.touch()
        self.session_path = path
        return path

    # ---- L2 会话日志 ---------------------------------------------------------

    def append(self, message: Message) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.session_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message.to_record(), ensure_ascii=False) + "\n")

    def load(self, limit: int = MAX_HISTORY) -> list[Message]:
        if not self.session_path.is_file():
            return []
        messages: list[Message] = []
        with self.session_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    messages.append(Message.from_record(record))
        return messages[-limit:]

    # ---- L3 长期记忆 ---------------------------------------------------------

    def profile(self) -> dict:
        if not self.profile_path.is_file():
            return {}
        try:
            data = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def remember(self, facts: dict) -> None:
        profile = self.profile()
        profile.update(facts)
        profile["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile_path.write_text(
            json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def profile_text(self) -> str:
        profile = self.profile()
        lines: list[str] = []
        facts = profile.get("facts")
        if isinstance(facts, dict):
            lines += [f"- {key}：{value}" for key, value in facts.items()]
        preferences = profile.get("preferences")
        if isinstance(preferences, list):
            lines += [f"- {item}" for item in preferences]
        return "\n".join(lines)

    def summary_text(self) -> str:
        if not self.summary_path.is_file():
            return ""
        try:
            return self.summary_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def write_summary(self, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text(text.strip() + "\n", encoding="utf-8")

    def recall(self, query: str, k: int = 5) -> list[str]:
        """预留接口：现在是关键词匹配，将来可替换成向量检索而不改调用方。"""
        text = self.profile_text()
        if not text or not query.strip():
            return []
        words = [word for word in query.replace("，", " ").replace(",", " ").split() if word]
        hits = [line for line in text.splitlines() if any(word in line for word in words)]
        return hits[:k]

    # ---- L1 上下文组装 -------------------------------------------------------

    def build_context(self, system_prompt: str, limit: int) -> list[dict]:
        sections: list[str] = []
        if system_prompt.strip():
            sections.append(system_prompt.strip())
        profile = self.profile_text()
        if profile:
            sections.append(f"关于用户的长期记忆：\n{profile}")
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
