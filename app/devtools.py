"""开发者面板的数据来源：一份内存里的观察记录 + 应用日志。

只在「设置 → 开发者」里打开开关后才记录；关掉时所有入口都是空转，平时零开销。
对话记录只放在内存里（含完整提示词，量大），**日志另外落一份到本地文件**
（``data/devtools.log``）：量小、事后能翻，面板上的「清除缓存」会把它删掉。

这里刻意不依赖 Qt：面板窗口订阅 :func:`subscribe` 拿增量，其它模块只管调
:func:`record` 与 :data:`log`。
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Any, Callable

from app import paths

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
    """清空内存记录，并删掉本地日志文件（面板上的「清除缓存」走这里）。"""
    _records.clear()
    try:
        paths.DEVTOOLS_LOG_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    _notify({"kind": "clear", "ts": stamp()})


def saved_lines(limit: int = 500) -> list[str]:
    """本地日志文件里最后若干行（面板打开时拿来铺底，接上前几次运行的记录）。"""
    path = paths.DEVTOOLS_LOG_PATH
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return lines[-limit:]


def _write_line(line: str) -> None:
    try:
        paths.DEVTOOLS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with paths.DEVTOOLS_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


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


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数，只用于看量级——不追求与计费口径一致。

    不引入分词器（项目一直保持零新增依赖），用两条经验规则：

    * **非 ASCII 字符按 1 token 计**：常用汉字、中文标点与全角符号在 BPE 里
      基本各自成词，实测对纯中文文本误差在 ±15% 以内；
    * **ASCII 按 4 字符 1 token 计**：英文单词与数字的平均水平。

    所以它适合比较"哪一段更贵"，不适合拿来对账。
    """
    if not text:
        return 0
    wide = sum(1 for char in text if ord(char) > 0x7F)
    narrow = len(text) - wide
    return wide + round(narrow / 4)


def record(kind: str, **fields: Any) -> None:
    """记一条。未开启时直接返回，所以调用方不必到处写 ``if enabled()``。"""
    if not _enabled:
        return
    event: dict[str, Any] = {"kind": kind, "ts": stamp()}
    event.update(fields)
    _records.append(event)
    _notify(event)


def preview(kind: str, **fields: Any) -> None:
    """发一条**不记录**的临时事件：描述"正在进行中"的状态。

    和 :func:`record` 的分工：流式内容动辄上百次更新，全塞进历史会把记录挤爆，
    也没法复原（重开面板时该看到的是最终结果）。所以这里只通知订阅者，收尾时那条
    :func:`record` 才是权威数据——面板据它重画。
    """
    if not _enabled:
        return
    event: dict[str, Any] = {"kind": kind, "ts": stamp(), "preview": True}
    event.update(fields)
    _notify(event)


def _notify(event: dict[str, Any]) -> None:
    for callback in list(_listeners):
        try:
            callback(event)
        except Exception:  # noqa: BLE001 - 面板出问题不能拖累主流程
            continue


class _PanelHandler(logging.Handler):
    """把日志收进同一份记录，面板里就能一处看全；同时落一行到本地文件。"""

    def emit(self, record_: logging.LogRecord) -> None:
        if not _enabled:
            return
        try:
            text = record_.getMessage()
            if record_.exc_info:
                text = f"{text}\n{logging.Formatter().formatException(record_.exc_info)}"
        except Exception:  # noqa: BLE001
            return
        text = text[:MAX_LOG_CHARS]
        # 行格式和面板里显示的一致，重开面板时从文件读回来不会有割裂感
        _write_line(f"[{datetime.now().strftime('%H:%M:%S')}] {record_.levelname:<7} {text}")
        record("log", level=record_.levelname, logger=record_.name, text=text)


def configure_logging(level: int = logging.INFO) -> None:
    """程序启动时调用一次：日志同时写控制台与开发者面板。"""
    if log.handlers:
        return
    log.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    log.addHandler(console)
    log.addHandler(_PanelHandler())
