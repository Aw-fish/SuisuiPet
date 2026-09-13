"""OpenAI 兼容的流式 ``chat/completions`` 实现。

DeepSeek、OpenAI 以及各种兼容中转、本地服务都可以直接用。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Iterator, Sequence

import requests

from app.conversation.providers.base import Chunk, ProviderError, ToolCall

CONNECT_TIMEOUT = 10
PROBE_TIMEOUT = 20

_STATUS_HINTS = {
    400: "请求格式不正确",
    401: "鉴权失败，请检查环境变量里的 API Key",
    402: "账户余额不足",
    403: "没有访问该模型的权限",
    404: "接口地址或模型名称不正确",
    429: "请求过于频繁，稍后再试",
    500: "服务端错误",
    503: "服务暂时不可用",
}


class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        temperature: float = 0.8,
        timeout: int = 60,
        proxy: str = "",
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = (model or "").strip()
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.proxy = (proxy or "").strip()
        self._response: requests.Response | None = None
        self._lock = threading.Lock()
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        """默认忽略系统 / 环境变量代理。

        Windows 上 ``requests`` 会自动读取注册表里的 WinINET 代理设置，那通常是
        为别的用途配的（实测会让国内直连的接口报 SSLEOFError）。需要代理时在
        角色设置里显式填写。
        """
        session = requests.Session()
        session.trust_env = False
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        return session

    # ---- 请求构造 -----------------------------------------------------------

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _validate(self) -> None:
        if not self.base_url:
            raise ProviderError("没有配置 API 地址")
        if not self.model:
            raise ProviderError("没有配置模型名称")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, messages: Sequence[dict], tools: Sequence[dict] | None, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.temperature,
            "stream": stream,
        }
        if tools:
            payload["tools"] = list(tools)
        return payload

    # ---- 流式对话 -----------------------------------------------------------

    def stream(
        self,
        messages: Sequence[dict],
        tools: Sequence[dict] | None = None,
        cancel: threading.Event | None = None,
    ) -> Iterator[Chunk]:
        self._validate()
        try:
            response = self._session.post(
                self.endpoint,
                headers=self._headers(),
                json=self._payload(messages, tools, True),
                stream=True,
                timeout=(CONNECT_TIMEOUT, self.timeout),
            )
        except requests.RequestException as exc:
            raise ProviderError(self._network_error(exc)) from exc
        with self._lock:
            self._response = response
        try:
            if response.status_code >= 400:
                raise ProviderError(self._error_text(response))
            yield from self._iter_chunks(response, cancel)
        finally:
            with self._lock:
                self._response = None
            response.close()

    def abort(self) -> None:
        """从其它线程断开连接，让阻塞中的读取立刻返回。"""
        with self._lock:
            response = self._response
        if response is not None:
            response.close()

    def complete(
        self,
        messages: Sequence[dict],
        max_tokens: int = 800,
        timeout: int = 40,
        temperature: float | None = None,
    ) -> str:
        """非流式一次拿回完整回复，用于记忆抽取等一次性任务。"""
        self._validate()
        payload = self._payload(messages, None, False)
        payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        try:
            response = self._session.post(
                self.endpoint,
                headers=self._headers(),
                json=payload,
                timeout=(CONNECT_TIMEOUT, timeout),
            )
        except requests.RequestException as exc:
            raise ProviderError(self._network_error(exc)) from exc
        try:
            if response.status_code >= 400:
                raise ProviderError(self._error_text(response))
            body = response.json()
        finally:
            response.close()
        choices = body.get("choices") or []
        if not choices:
            raise ProviderError("返回内容为空")
        message = choices[0].get("message") or {}
        return str(message.get("content") or "").strip()

    def probe(self) -> str:
        """发一条最小请求验证地址 / Key / 模型名，成功时返回模型名。"""
        self._validate()
        try:
            response = self._session.post(
                self.endpoint,
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "stream": False,
                },
                timeout=(CONNECT_TIMEOUT, PROBE_TIMEOUT),
            )
        except requests.RequestException as exc:
            raise ProviderError(self._network_error(exc)) from exc
        if response.status_code >= 400:
            raise ProviderError(self._error_text(response))
        return self.model

    @staticmethod
    def _network_error(exc: Exception) -> str:
        """把底层网络异常翻译成可操作的提示。"""
        text = str(exc)
        if "proxy" in text.lower():
            return (
                f"连接失败：{text}\n"
                "看起来是系统代理在阻断请求。可以在「角色设置 → 代理」里填写正确的代理地址，"
                "或确认该接口是否可以直连。"
            )
        return f"连接失败：{text}"

    # ---- SSE 解析 -----------------------------------------------------------

    def _iter_chunks(self, response: requests.Response, cancel: threading.Event | None) -> Iterator[Chunk]:
        pending: dict[int, dict[str, str]] = {}
        for raw in response.iter_lines(decode_unicode=True):
            if cancel is not None and cancel.is_set():
                return
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            delta = choices[0].get("delta") or {}
            self._collect_tool_calls(delta.get("tool_calls"), pending)
            text = delta.get("content") or ""
            if text:
                yield Chunk(delta=text)
        yield Chunk(tool_calls=self._finish_tool_calls(pending), finished=True)

    @staticmethod
    def _collect_tool_calls(raw_calls: Any, pending: dict[int, dict[str, str]]) -> None:
        if not isinstance(raw_calls, list):
            return
        for item in raw_calls:
            if not isinstance(item, dict):
                continue
            slot = pending.setdefault(int(item.get("index", 0)), {"id": "", "name": "", "arguments": ""})
            if item.get("id"):
                slot["id"] = str(item["id"])
            function = item.get("function") or {}
            if function.get("name"):
                slot["name"] = str(function["name"])
            if function.get("arguments"):
                slot["arguments"] += str(function["arguments"])

    @staticmethod
    def _finish_tool_calls(pending: dict[int, dict[str, str]]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index in sorted(pending):
            slot = pending[index]
            if not slot["name"]:
                continue
            try:
                arguments = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        return calls

    @staticmethod
    def _error_text(response: requests.Response) -> str:
        detail = ""
        try:
            body = response.json()
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            if not detail:
                detail = json.dumps(body, ensure_ascii=False)[:200]
        except ValueError:
            detail = (response.text or "").strip()[:200]
        hint = _STATUS_HINTS.get(response.status_code)
        if hint:
            return f"{response.status_code} {hint}：{detail}"
        return f"{response.status_code}：{detail or '请求失败'}"
