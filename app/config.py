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
    "你需要进行角色扮演，严格遵守【角色设定】中的内容，在日常对话中进行沉浸式扮演。对话与回应规范如下：\n"
    "- 思考与回复都使用第一人称视角\n"
    "- 语言风格口语化，减少书面语和频繁的情感词\n"
    "- 仅输出角色语言，不描述动作或心理\n"
    "- 单次回应以短语或短句为主，不超过四句，控制在60字以内\n"
    "- 仅输出纯文本，不要使用markdown语法、不要换行\n"
    "- 不要编造事实"
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "character": {"selected": "Suisui", "registered": ["Suisui"]},
    "conversation": {
        "base_prompt": BASE_SYSTEM_PROMPT,
        #: 让模型在回复里内嵌 [开心] 这类标记，立绘随之换表情
        "auto_expression": True,
        #: 表情切换灵敏度 1~10（越大越跟手，见 app/pet/emotion.py 的参数表）
        "expression_sensitivity": 5,
        #: 对话形式：``window`` 聊天窗口 / ``bubble`` 漂浮输入框 + 角色上方的对白气泡
        "reply_form": "window",
        #: 对白气泡停留秒数（3~30），只在对白形式下有意义
        "bubble_seconds": 8,
        #: 对白气泡背景的不透明度（30~100，百分比），只在对白形式下有意义
        "bubble_opacity": 80,
    },
    "motion": {"mode": "stationary"},
    "tools": {
        "pomodoro_minutes": 25,
        "weather_city": "",
        "quick_note_hint": True,
        #: 记事板背景不透明度（30~100，百分比）
        "notes_opacity": 90,
        #: 记事板每次打开的落点：``pet`` 角色旁 / ``corner`` 右下角 / ``center`` 屏幕中间
        "notes_position": "pet",
    },
    #: 开发者面板：只看内存里的观察记录，不写文件（见 app/devtools.py）
    "developer": {"enabled": False},
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
