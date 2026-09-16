"""全局设置与持久化。

``data/settings.json`` 只保存全局偏好与角色注册表；每个角色的具体配置存在
``data/characters/<名字>/character.json``（见 :mod:`app.characters`）。
旧版把角色配置内嵌在 settings.json 里的结构会在加载时自动迁移。
"""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

from app import characters
from app.paths import SETTINGS_PATH

#: 与角色无关的通用说话约束。组装 system 消息时拼在角色提示词前面，
#: 让所有角色都"像在聊天"，而不是写小作文（见 ConversationService._system_prompt）。
BASE_SYSTEM_PROMPT = (
    "你在用即时通讯工具和用户聊天，请像真人发消息一样回复：\n"
    "1. 用口语化的短句，不要写成客服话术或说明书；\n"
    "2. 每次回复控制在 1~3 句、大约 60 字以内，不要长篇大论；\n"
    "3. 只输出纯文本，不要使用 Markdown 语法（#、*、-、反引号、表格、代码块等）；\n"
    "4. 不要分段、不要列清单、不要加小标题，整条回复连成一段；\n"
    "5. 不要复述或总结用户的话，直接回应；\n"
    "6. 不知道就直说不知道，不要编造事实。"
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "character": {"selected": "Suisui", "registered": ["Suisui"]},
    "conversation": {"base_prompt": BASE_SYSTEM_PROMPT},
    "motion": {"mode": "stationary"},
    "tools": {"pomodoro_minutes": 25, "weather_city": "", "quick_note_hint": True},
}


def _merge(target: dict[str, Any], saved: dict[str, Any]) -> None:
    """Recursively merge saved values into the defaults template."""
    for key, value in saved.items():
        if isinstance(target.get(key), dict) and isinstance(value, dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _migrate_character_entry(name: str, info: dict[str, Any]) -> str:
    """把旧版内嵌的角色配置搬进 ``data/characters/<名字>/``。"""
    real = characters.ensure_character(name)
    sprites = characters.sprite_dir(real)
    source = str(info.get("asset_path", "")).strip()
    if source:
        source_path = Path(source)
        if source_path.is_dir() and source_path.resolve() != sprites.resolve():
            sprites.mkdir(parents=True, exist_ok=True)
            for entry in sorted(source_path.iterdir()):
                if not entry.is_file():
                    continue
                target = sprites / entry.name
                if target.exists():
                    target.unlink()
                shutil.move(str(entry), str(target))
            try:
                source_path.rmdir()
            except OSError:
                pass
    characters.save_character(
        real,
        {
            "name": real,
            "format": info.get("format", "img"),
            "asset": characters.SPRITE_DIRNAME,
            "asset_path": "",
            "model": info.get("model", ""),
            "system_prompt": info.get("system_prompt", ""),
            "activity": info.get("activity", 5),
        },
    )
    return real


def _migrate_legacy_characters(saved: dict[str, Any]) -> None:
    """旧结构把角色数据内嵌在 settings.json，这里一次性拆分到角色文件夹。"""
    character = saved.get("character")
    if not isinstance(character, dict) or "items" not in character:
        return
    items = character.pop("items")
    registered: list[str] = []
    if isinstance(items, dict):
        for raw_name, raw_info in items.items():
            info = raw_info if isinstance(raw_info, dict) else {}
            registered.append(_migrate_character_entry(str(raw_name), info))
    character["registered"] = registered
    if character.get("selected") not in registered:
        character["selected"] = registered[0] if registered else ""


def _normalize(settings: dict[str, Any]) -> None:
    """保证注册表里只剩真实存在的角色，并选出一个有效角色。"""
    character = settings.setdefault("character", {})
    registered: list[str] = []
    for raw in character.get("registered") or []:
        name = str(raw).strip()
        if name and name not in registered and characters.character_dir(name).is_dir():
            registered.append(name)
    if not registered:
        registered = [str(character.get("selected") or "").strip() or "Suisui"]
    character["registered"] = registered
    if character.get("selected") not in registered:
        character["selected"] = registered[0]


def load_settings() -> dict[str, Any]:
    """Load user settings, falling back to safe defaults."""
    settings = deepcopy(DEFAULT_SETTINGS)
    if not SETTINGS_PATH.is_file():
        return settings
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    if not isinstance(saved, dict):
        return settings
    _migrate_legacy_characters(saved)
    for group, values in saved.items():
        if group in settings and isinstance(values, dict):
            _merge(settings[group], values)
    _normalize(settings)
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    """Persist global settings inside the project data directory."""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
