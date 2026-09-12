"""Default settings and persistence helpers."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT_DIR / "data" / "settings.json"

DEFAULT_CHARACTER: dict[str, Any] = {
    "format": "img",
    "asset_path": "",
    "model": "",
    "api_key": "",
    "system_prompt": "",
    "activity": 5,
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "character": {
        "selected": "Suisui",
        "items": {"Suisui": deepcopy(DEFAULT_CHARACTER)},
    },
    "conversation": {"show_floating_dialog": False},
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


def _migrate_legacy(saved: dict[str, Any]) -> None:
    """Move pre-per-character conversation fields into a character entry."""
    character = saved.get("character")
    conversation = saved.get("conversation")
    if not isinstance(character, dict) or not isinstance(conversation, dict):
        return
    if "items" in character:
        return
    name = character.get("selected") or "Suisui"
    character["items"] = {
        name: {
            "format": "img",
            "asset_path": "",
            "model": conversation.get("model", ""),
            "api_key": conversation.get("api_key", ""),
            "system_prompt": conversation.get("system_prompt", ""),
        }
    }
    for key in ("model", "api_key", "system_prompt", "api_url"):
        conversation.pop(key, None)


def _normalize_characters(settings: dict[str, Any]) -> None:
    """Ensure every character entry carries the full default field set."""
    items = settings["character"].setdefault("items", {})
    for name, info in list(items.items()):
        merged = deepcopy(DEFAULT_CHARACTER)
        if isinstance(info, dict):
            merged.update(info)
        items[name] = merged
    if not items:
        items["Suisui"] = deepcopy(DEFAULT_CHARACTER)
    if settings["character"].get("selected") not in items:
        settings["character"]["selected"] = next(iter(items))


def load_settings() -> dict[str, Any]:
    """Load user settings, falling back to safe defaults."""
    settings = deepcopy(DEFAULT_SETTINGS)
    if not SETTINGS_PATH.exists():
        return settings
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    if not isinstance(saved, dict):
        return settings
    _migrate_legacy(saved)
    for group, values in saved.items():
        if group in settings and isinstance(values, dict):
            _merge(settings[group], values)
    _normalize_characters(settings)
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    """Persist settings inside the project data directory."""
    SETTINGS_PATH.parent.mkdir(exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


