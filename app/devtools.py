"""开发者面板的数据来源：一份内存里的观察记录 + 应用日志。

只在「设置 → 开发者」里打开开关后才记录；关掉时所有入口都是空转，平时零开销。
记录只放在内存里、不写文件——它是用来看"程序此刻在做什么"的，不是审计日志。

这里刻意不依赖 Qt：面板窗口订阅 :func:`subscribe` 拿增量，其它模块只管调
:func:`record` 与 :data:`log`。
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Any, Callable

#: 最多保留多少条记录（面板看的是近期行为，不做持久化）
MAX_RECORDS = 500
#: 单条日志最多保留多少字符，免得一条超长日志把面板塞满
MAX_LOG_CHARS = 4000

#: 应用日志统一挂在这个名字下（见 :func:`configure_logging`）
log = logging.getLogger("suisui")

_records: deque[dict[str, Any]] = deque(maxlen=MAX_RECORDS)
_listeners: list[Callable[[dict[str, Any]], None]] = []
_enabled = False


def enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    """开关记录。切换时清空已有内容，免得下次打开看到一堆旧东西。"""
    global _enabled
    value = bool(value)
    if value == _enabled:
        return
    _enabled = value
    clear()
    log.info("开发者面板 %s", "已开启" if value else "已关闭")


def clear() -> None:
    _records.clear()
    _notify({"kind": "clear", "ts": stamp()})


def records() -> list[dict[str, Any]]:
    """当前留存的全部记录（面板初次打开时用它铺满）。"""
    return list(_records)


def subscribe(callback: Callable[[dict[str, Any]], None]) -> None:
    if callback not in _listeners:
        _listeners.append(callback)


def unsubscribe(callback: Callable[[dict[str, Any]], None]) -> None:
    if callback in _listeners:
        _listeners.remove(callback)


def stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record(kind: str, **fields: Any) -> None:
    """记一条。未开启时直接返回，所以调用方不必到处写 ``if enabled()``。"""
    if not _enabled:
        return
    event: dict[str, Any] = {"kind": kind, "ts": stamp()}
    event.update(fields)
    _records.append(event)
    _notify(event)


def _notify(event: dict[str, Any]) -> None:
    for callback in list(_listeners):
        try:
            callback(event)
        except Exception:  # noqa: BLE001 - 面板出问题不能拖累主流程
            continue


class _PanelHandler(logging.Handler):
    """把日志收进同一份记录，面板里就能一处看全。"""

    def emit(self, record_: logging.LogRecord) -> None:
        if not _enabled:
            return
        try:
            text = record_.getMessage()
            if record_.exc_info:
                text = f"{text}\n{logging.Formatter().formatException(record_.exc_info)}"
        except Exception:  # noqa: BLE001
            return
        record("log", level=record_.levelname, logger=record_.name, text=text[:MAX_LOG_CHARS])


def configure_logging(level: int = logging.INFO) -> None:
    """程序启动时调用一次：日志同时写控制台与开发者面板。"""
    if log.handlers:
        return
    log.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    log.addHandler(console)
    log.addHandler(_PanelHandler())
