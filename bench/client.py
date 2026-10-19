"""Клиент для замеров: один запрос chat/completions с таймингами по чанкам.

В отличие от agent/llm.py не бросает исключений на HTTP-ошибках — бенчмарку контекста
нужно разбирать текст ошибки («maximum context length is …»). Подробности — docs/wiki/bench/README.md.
"""

import json
import time
from dataclasses import dataclass, field

import httpx

from agent.llm import ToolCallBuilder, _error_message


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    status: int = 0                 # HTTP-код; 0 — не дошли до ответа (сеть, таймаут)
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_reported: bool = False    # апстрим прислал usage (иначе completion_tokens — оценка по чанкам)
    ttft: float | None = None       # с от отправки до первого содержательного чанка
    total: float = 0.0              # с от отправки до конца ответа
    chunks: int = 0
    finish_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error

    @property
    def decode_tps(self) -> float | None:
        """Скорость генерации: токены после первого / время после первого токена."""
        if self.ttft is None or self.completion_tokens < 2 or self.total <= self.ttft:
            return None
        return (self.completion_tokens - 1) / (self.total - self.ttft)

    @property
    def prefill_tps(self) -> float | None:
        """Грубая скорость чтения промпта: prompt_tokens / TTFT (включает сеть и очередь)."""
        if not self.ttft or not self.prompt_tokens:
            return None
        return self.prompt_tokens / self.ttft


class BenchClient:
    def __init__(self, base_url: str, *, token: str = "", headers: dict | None = None,
                 timeout: float = 300, transport: httpx.AsyncBaseTransport | None = None,
                 max_connections: int = 64):
        h = dict(headers or {})
        if token:
            h["authorization"] = f"Bearer {token}"
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=h, trust_env=False, transport=transport,
            timeout=httpx.Timeout(timeout, connect=15),
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections))

    async def close(self) -> None:
        await self.http.aclose()

    async def models(self) -> list[dict]:
        resp = await self.http.get("/models")
        if resp.status_code != 200:
            raise RuntimeError(f"/models: {resp.status_code}: {_error_message(resp.content)}")
        return [m for m in resp.json().get("data", []) if isinstance(m, dict)]

    async def complete(self, model: str, messages: list[dict], *, max_tokens: int | None = 256,
                       temperature: float | None = 0.0, tools: list[dict] | None = None,
                       extra: dict | None = None) -> Completion:
        body: dict = {"model": model, "messages": messages, "stream": True,
                      "stream_options": {"include_usage": True}}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = tools
        body.update(extra or {})
        out = Completion()
        started = time.perf_counter()
        try:
            req = self.http.build_request("POST", "/chat/completions", json=body)
            resp = await self.http.send(req, stream=True)
        except httpx.HTTPError as exc:
            out.error, out.total = f"{type(exc).__name__}: {exc}", time.perf_counter() - started
            return out
        out.status = resp.status_code
        try:
            if resp.status_code != 200:
                out.error = _error_message(await resp.aread())
                return out
            if not resp.headers.get("content-type", "").startswith("text/event-stream"):
                data = json.loads(await resp.aread())
                out.ttft = time.perf_counter() - started
                msg = ((data.get("choices") or [{}])[0]).get("message") or {}
                out.text, out.tool_calls = msg.get("content") or "", msg.get("tool_calls") or []
                self._usage(out, data)
                return out
            calls, parts = ToolCallBuilder(), []
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
                    out.error = _error_message(payload.encode())
                    break
                if chunk.get("usage"):
                    self._usage(out, chunk)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content") or delta.get("reasoning_content") or ""
                    if piece or delta.get("tool_calls"):
                        if out.ttft is None:
                            out.ttft = time.perf_counter() - started
                        out.chunks += 1
                    if delta.get("content"):
                        parts.append(delta["content"])
                    if delta.get("tool_calls"):
                        calls.add(delta["tool_calls"])
                    out.finish_reason = choice.get("finish_reason") or out.finish_reason
            out.text, out.tool_calls = "".join(parts), calls.result()
        except httpx.HTTPError as exc:
            out.error = f"обрыв: {type(exc).__name__}: {exc}"
        except ValueError as exc:
            out.error = f"неверный ответ: {exc}"
        finally:
            await resp.aclose()
            out.total = time.perf_counter() - started
            if not out.usage_reported:
                out.completion_tokens = out.chunks  # оценка: чанк ≈ токен
        return out

    @staticmethod
    def _usage(out: Completion, data: dict) -> None:
        u = data.get("usage") or {}
        out.prompt_tokens = int(u.get("prompt_tokens") or 0)
        out.completion_tokens = int(u.get("completion_tokens") or 0)
        out.usage_reported = True
