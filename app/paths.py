"""Shared filesystem locations.

单独成模块是为了打断 ``app.config`` 与 ``app.characters`` 之间的循环依赖。
"""

from __future__ import annotations

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"
#: 角色总目录，每个角色一个子文件夹
CHARACTERS_DIR = DATA_DIR / "characters"
#: 占位素材生成脚本的源图
SOURCE_ART_PATH = DATA_DIR / "img" / "Default.png"
#: 应用 / 任务栏图标（由 tools/make_icon.py 生成）
ICON_PATH = DATA_DIR / "icon.ico"
ICON_PNG_PATH = DATA_DIR / "icon.png"
#: 开发者面板的运行日志（本地文件，不进版本控制；面板上的「清除缓存」会删掉它）
DEVTOOLS_LOG_PATH = DATA_DIR / "devtools.log"
#: 记事板内容（纯文本，本地文件，不进版本控制）
NOTES_PATH = DATA_DIR / "notes.md"
