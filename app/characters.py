"""角色目录（``data/characters/<名字>/``）的创建、导入、删除与配置读写。

每个角色一个独立文件夹，把立绘、对话配置与记忆收在一起，整体拷走即可迁移：

    data/characters/<名字>/
    ├── character.json      # 角色配置（app/characters.py 负责读写）
    ├── sprites/            # img 格式立绘
    └── memory/             # 对话记忆（对话功能写入）

``settings.json`` 里只保留 ``character.selected`` 与 ``character.registered``
这份"注册表"，因此删除角色可以区分「断开连接」（保留文件夹）与「删除数据」
（连同立绘与记忆一起删除）。
"""

from __future__ import annotations

import json
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.paths import CHARACTERS_DIR

CONFIG_NAME = "character.json"
SPRITE_DIRNAME = "sprites"
MEMORY_DIRNAME = "memory"

#: API Key 存在这里，方便本地改动；character.json 已在 .gitignore 中忽略，
#: 不会随仓库上传
DEFAULT_CHARACTER: dict[str, Any] = {
    "name": "",
    "format": "img",
    "asset": SPRITE_DIRNAME,
    "asset_path": "",
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "proxy": "",
    "temperature": 0.8,
    "max_context_messages": 20,
    "system_prompt": "",
    "activity": 5,
}

_ILLEGAL_NAME = re.compile(r'[\\/:*?"<>|]')


def is_valid_name(name: str) -> bool:
    """角色名会直接作为文件夹名，需要挡掉非法字符。"""
    name = name.strip()
    if not name or name in {".", ".."}:
        return False
    return _ILLEGAL_NAME.search(name) is None


def next_available_name(base: str) -> str:
    """重名时追加 -2、-3……"""
    base = base.strip()
    if not (CHARACTERS_DIR / base).exists():
        return base
    index = 2
    while (CHARACTERS_DIR / f"{base}-{index}").exists():
        index += 1
    return f"{base}-{index}"


def character_dir(name: str) -> Path:
    return CHARACTERS_DIR / name


def config_path(name: str) -> Path:
    return character_dir(name) / CONFIG_NAME


def sprite_dir(name: str, info: dict[str, Any] | None = None) -> Path:
    """立绘目录：默认是角色目录下的 ``sprites``，``asset`` 支持绝对路径。"""
    asset = str((info or {}).get("asset") or "").strip() or SPRITE_DIRNAME
    directory = Path(asset)
    return directory if directory.is_absolute() else character_dir(name) / directory


def memory_dir(name: str) -> Path:
    return character_dir(name) / MEMORY_DIRNAME


def registered_folders() -> list[str]:
    """磁盘上实际存在（且带 character.json）的角色名。"""
    if not CHARACTERS_DIR.is_dir():
        return []
    return sorted(
        entry.name
        for entry in CHARACTERS_DIR.iterdir()
        if entry.is_dir() and (entry / CONFIG_NAME).is_file()
    )


def load_character(name: str) -> dict[str, Any]:
    """读取角色配置，缺失或损坏时回落到默认值。"""
    info = deepcopy(DEFAULT_CHARACTER)
    info["name"] = name
    path = config_path(name)
    if not path.is_file():
        return info
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return info
    if isinstance(saved, dict):
        info.update(saved)
    info["name"] = name
    return info


def save_character(name: str, info: dict[str, Any]) -> None:
    """把角色配置写回 ``character.json``，未提供的字段用默认值补齐。"""
    character_dir(name).mkdir(parents=True, exist_ok=True)
    payload = deepcopy(DEFAULT_CHARACTER)
    payload.update(info)
    payload["name"] = name
    config_path(name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def ensure_character(name: str) -> str:
    """保证角色文件夹与配置存在（幂等），返回实际使用的名字。"""
    if not is_valid_name(name):
        name = "Suisui"
    directory = character_dir(name)
    if not directory.is_dir():
        directory.mkdir(parents=True, exist_ok=True)
    sprite_dir(name).mkdir(parents=True, exist_ok=True)
    memory_dir(name).mkdir(parents=True, exist_ok=True)
    if not config_path(name).is_file():
        save_character(name, {"name": name})
    return name


def create_character(name: str) -> str:
    """新建角色，返回实际使用的名字（重名会自动加后缀）。"""
    real = ensure_character(next_available_name(name.strip()))
    return real


def import_character(source: Path | str) -> str:
    """把一个已有的角色文件夹复制进 ``data/characters/``，返回角色名。"""
    source = Path(source)
    if not source.is_dir():
        raise ValueError("请选择一个角色文件夹")
    source_config = source / CONFIG_NAME
    if source.resolve() == CHARACTERS_DIR.resolve():
        raise ValueError("请选择具体的角色文件夹，而不是 characters 总目录")
    real = next_available_name(source.name)
    target = character_dir(real)
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    if not source_config.is_file():
        # 导入的目录没有配置文件时，按文件夹名建一份默认配置
        save_character(real, {"name": real})
    ensure_character(real)
    return real


def delete_character_data(name: str) -> None:
    """彻底删除角色文件夹（立绘 + 配置 + 记忆）。"""
    shutil.rmtree(character_dir(name), ignore_errors=True)
