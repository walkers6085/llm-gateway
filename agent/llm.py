"""Клиент chat/completions (OpenAI API) со стримингом и сборкой tool_calls.

Работает поверх любого `httpx.AsyncClient`: на сервере — mTLS-клиент шлюза
(`app.state.client`, base_url = апстрим), в CLI — клиент с base_url шлюза и токеном gw-….
"""

import json
from collections.abc import AsyncIterator
from contextlib import aclosing

import httpx

from .streaming import data_events
from .tracing import current


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
                 headers: dict[str, str] | None = None, options: dict | None = None, max_tokens: int = 1024, limiter=None):
        self.http, self.path, self.headers = http, path, headers or {}
        self.base_options = dict(options or {})
        self.options, self.max_tokens = dict(self.base_options), max_tokens
        self.limiter = limiter

    async def stream(self, model, messages, tools=None, temperature=None):
        sem = self.limiter(model) if self.limiter else None
        if sem is not None:
            await sem.acquire()
        try:
            async with aclosing(self._stream(model, messages, tools, temperature)) as events:
                async for event in events:
                    yield event
        finally:
            if sem is not None:
                sem.release()

    async def _stream(self, model: str, messages: list[dict], tools: list[dict] | None = None,
                     temperature: float | None = None) -> AsyncIterator[dict]:
        """События: {"type": "text", "text"} … и в конце {"type": "message", "message", "usage", "finish_reason"}."""
        streaming = self.options.get('stream', True)
        body: dict = {"model": model, "messages": messages, "stream": streaming,
                      self.options.get('max_tokens_field', 'max_tokens'): self.max_tokens}
        if streaming and self.options.get('usage', True):
            body['stream_options'] = {'include_usage': True}
        if tools and not self.options.get('tools', True):
            raise LLMError(400, 'модель не поддерживает инструменты; отключите tools.enabled')
        if tools:
            body["tools"] = tools
        if temperature is not None and self.options.get("temperature", True):
            body["temperature"] = temperature
        trace = current.get()
        trace_headers = ({"X-Request-ID": trace.request_id, "X-Span-ID": trace.span_id,
                          "X-Parent-Span-ID": trace.parent_span_id} if trace else {})
        request_headers = httpx.Headers({"accept": "text/event-stream, application/json", **self.headers})
        request_headers.update(trace_headers)
        request = self.http.build_request("POST", self.path, json=body, headers=request_headers)
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
                choice = data["choices"][0]
                msg = choice["message"]
                if not isinstance(msg, dict):
                    raise ValueError('message должен быть объектом')
                check_finish(choice.get('finish_reason'), bool(msg.get('tool_calls')))
                check_calls(msg.get('tool_calls') or [])
                reasoning = reasoning_text(msg)
                if reasoning:
                    yield {"type": "reasoning", "text": reasoning}
                if msg.get("content"):
                    yield {"type": "text", "text": msg["content"]}
                yield {"type": "message", "message": _assistant(msg.get("content") or "", msg.get("tool_calls") or [], reasoning),
                       "usage": _usage(data), "finish_reason": choice.get("finish_reason")}
                return
            text, calls, usage, finish = [], ToolCallBuilder(), (0, 0), None
            reasoning = []
            done = False
            received = 0
            async for payload in data_events(resp):
                received += len(payload.encode())
                if received > min(16*1024*1024, self.max_tokens*1024+1048576):
                    raise LLMError(502, 'слишком большой ответ модели')
                payload = payload.strip()
                if payload == "[DONE]":
                    done = True
                    break
                chunk = json.loads(payload)
                if chunk.get("error"):
                    raise LLMError(502, _error_message(payload.encode()))
                if chunk.get("usage"):
                    usage = _usage(chunk)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    thought = reasoning_text(delta)
                    if thought:
                        reasoning.append(thought)
                        yield {"type": "reasoning", "text": thought}
                    if delta.get("content"):
                        text.append(delta["content"])
                        yield {"type": "text", "text": delta["content"]}
                    if delta.get("tool_calls"):
                        calls.add(delta["tool_calls"])
                    finish = choice.get("finish_reason") or finish
            if not done and finish is None:
                raise LLMError(502, 'незавершённый SSE: нет finish_reason или [DONE]')
            check_finish(finish, bool(calls.calls))
            check_calls(calls.result())
            yield {"type": "message", "message": _assistant("".join(text), calls.result(), "".join(reasoning)),
                   "usage": usage, "finish_reason": finish}
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise LLMError(502, f"неверный ответ модели: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(502, f"обрыв ответа модели: {type(exc).__name__}: {exc}") from exc
        finally:
            await resp.aclose()

    async def models(self) -> list[str]:
        resp = await self.http.get("/models", headers=self.headers)
        if resp.status_code != 200:
            raise LLMError(resp.status_code, _error_message(resp.content))
        try:
            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                raise ValueError('ожидается массив data')
            return [m['id'] for m in data['data'] if isinstance(m, dict) and isinstance(m.get('id'), str)]
        except (ValueError, TypeError, KeyError) as exc:
            raise LLMError(502, 'неверный каталог моделей') from exc


def _assistant(content: str, tool_calls: list[dict], reasoning: str = "") -> dict:
    msg: dict = {"role": "assistant", "content": content or None}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _usage(data: dict) -> tuple[int, int]:
    u = data.get("usage") or {}
    return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def check_finish(reason, tools=False):
    if reason in ('length', 'content_filter'):
        raise LLMError(502, f'ответ модели не завершён: {reason}')
    if tools and reason != 'tool_calls':
        raise LLMError(502, 'вызовы инструментов не подтверждены finish_reason=tool_calls')
    if reason not in (None, 'stop', 'tool_calls'):
        raise LLMError(502, 'неизвестная причина завершения')


def check_calls(calls):
    ids = set()
    for call in calls:
        if not call.get('id') or call['id'] in ids or call.get('type') != 'function':
            raise ValueError('неверный id/type вызова')
        ids.add(call['id'])
        fn = call['function']
        if not fn.get('name') or not isinstance(fn.get('arguments'), (str, dict)):
            raise ValueError('неверные аргументы вызова')


def reasoning_text(message):
    value = message.get("reasoning_content") or message.get("reasoning") or ""
    return value if isinstance(value, str) else ""
