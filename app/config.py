"""Default settings and persistence helpers."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT_DIR / "data" / "settings.json"

DEFAULT_SETTINGS: dict[str, Any] = {
    "character": {"selected": "Suisui（占位角色）"},
    "conversation": {
        "model": "",
        "api_key": "",
        "show_floating_dialog": False,
    },
    "motion": {"mode": "stationary"},
    "tools": {"pomodoro_minutes": 25, "weather_city": "", "quick_note_hint": True},
}


def load_settings() -> dict[str, Any]:
    """Load user settings, falling back to safe defaults."""
    settings = deepcopy(DEFAULT_SETTINGS)
    if not SETTINGS_PATH.exists():
        return settings
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    for group, values in saved.items():
        if group in settings and isinstance(values, dict):
            settings[group].update(values)
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    """Persist settings inside the project data directory."""
    SETTINGS_PATH.parent.mkdir(exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


