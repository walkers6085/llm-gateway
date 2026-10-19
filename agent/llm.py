"""Клиент chat/completions (OpenAI API) со стримингом и сборкой tool_calls.

Работает поверх любого `httpx.AsyncClient`: на сервере — mTLS-клиент шлюза
(`app.state.client`, base_url = апстрим), в CLI — клиент с base_url шлюза и токеном gw-….
"""

import json
from collections.abc import AsyncIterator

import httpx


class LLMError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(f"{status}: {message}")


def _error_message(body: bytes) -> str:
    try:
        data = json.loads(body)
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("message") or json.dumps(err, ensure_ascii=False)
        return str(err or data.get("detail") or data)
    except (ValueError, AttributeError):
        return body.decode("utf-8", "replace")[:500] or "пустой ответ"


class ToolCallBuilder:
    """Склеивает дельты `tool_calls` (их приносят кусками по index) в полные вызовы."""

    def __init__(self):
        self.calls: dict[int, dict] = {}

    def add(self, deltas: list[dict]) -> None:
        for d in deltas:
            idx = d.get("index", len(self.calls))
            call = self.calls.setdefault(idx, {"id": "", "type": "function",
                                               "function": {"name": "", "arguments": ""}})
            if d.get("id"):
                call["id"] = d["id"]
            fn = d.get("function") or {}
            if fn.get("name"):
                call["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                args = fn["arguments"]
                call["function"]["arguments"] += args if isinstance(args, str) else json.dumps(args)

    def result(self) -> list[dict]:
        out = []
        for i, idx in enumerate(sorted(self.calls)):
            call = self.calls[idx]
            call["id"] = call["id"] or f"call_{i}"
            out.append(call)
        return out


class LLMClient:
    def __init__(self, http: httpx.AsyncClient, *, path: str = "/chat/completions",
                 headers: dict[str, str] | None = None):
        self.http, self.path, self.headers = http, path, headers or {}

    async def stream(self, model: str, messages: list[dict], tools: list[dict] | None = None,
                     temperature: float | None = None) -> AsyncIterator[dict]:
        """События: {"type": "text", "text"} … и в конце {"type": "message", "message", "usage", "finish_reason"}."""
        body: dict = {"model": model, "messages": messages, "stream": True,
                      "stream_options": {"include_usage": True}}
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature
        request = self.http.build_request("POST", self.path, json=body,
                                          headers={"accept": "text/event-stream, application/json", **self.headers})
        try:
            resp = await self.http.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise LLMError(502, f"модель недоступна: {type(exc).__name__}: {exc}") from exc
        try:
            if resp.status_code != 200:
                raise LLMError(resp.status_code, _error_message(await resp.aread()))
            if not resp.headers.get("content-type", "").startswith("text/event-stream"):
                # Апстрим проигнорировал stream=true — разбираем обычный ответ.
                data = json.loads(await resp.aread())
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                if msg.get("content"):
                    yield {"type": "text", "text": msg["content"]}
                yield {"type": "message", "message": _assistant(msg.get("content") or "", msg.get("tool_calls") or []),
                       "usage": _usage(data), "finish_reason": choice.get("finish_reason")}
                return
            text, calls, usage, finish = [], ToolCallBuilder(), (0, 0), None
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise LLMError(502, _error_message(payload.encode()))
                if chunk.get("usage"):
                    usage = _usage(chunk)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text.append(delta["content"])
                        yield {"type": "text", "text": delta["content"]}
                    if delta.get("tool_calls"):
                        calls.add(delta["tool_calls"])
                    finish = choice.get("finish_reason") or finish
            yield {"type": "message", "message": _assistant("".join(text), calls.result()),
                   "usage": usage, "finish_reason": finish}
        except httpx.HTTPError as exc:
            raise LLMError(502, f"обрыв ответа модели: {type(exc).__name__}: {exc}") from exc
        finally:
            await resp.aclose()

    async def models(self) -> list[str]:
        resp = await self.http.get("/models", headers=self.headers)
        if resp.status_code != 200:
            raise LLMError(resp.status_code, _error_message(resp.content))
        return [m.get("id") for m in resp.json().get("data", []) if m.get("id")]


def _assistant(content: str, tool_calls: list[dict]) -> dict:
    msg: dict = {"role": "assistant", "content": content or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _usage(data: dict) -> tuple[int, int]:
    u = data.get("usage") or {}
    return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
