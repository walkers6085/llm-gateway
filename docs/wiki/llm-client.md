# Клиент модели

Код: [`agent/llm.py`](../../agent/llm.py). Тесты: [`tests/test_llm.py`](../../tests/test_llm.py).

`LLMClient` ходит в `POST /chat/completions` (OpenAI API) через переданный `httpx.AsyncClient`:

| Где | HTTP-клиент | Заголовки |
|---|---|---|
| Сервер | `app.state.client` шлюза: base_url = `UPSTREAM_BASE_URL`, mTLS-сертификат | `upstream_headers()` (ключ апстрима) |
| CLI | `httpx.AsyncClient(base_url=gateway.url)` | `Authorization: Bearer gw-…` |

Так серверный агент идёт к инференсу напрямую (без лишнего HTTP-круга через себя), а CLI — через
шлюз и его токены, лимиты и учёт.

## Запрос

```json
{"model": "...", "messages": [...], "stream": true, "stream_options": {"include_usage": true},
 "tools": [{"type": "function", "function": {"name", "description", "parameters"}}],
 "temperature": 0.7}
```
`tools` не передаётся, если инструментов нет; `temperature` — если в настройках `null`.

## Разбор ответа

`stream()` отдаёт `{"type": "text", "text"}` по мере прихода и в конце одно
`{"type": "message", "message", "usage": (prompt, completion), "finish_reason"}`.

- `tool_calls` приходят фрагментами: первый кусок с `index`, `id`, `function.name`, дальше — куски
  `function.arguments`. `ToolCallBuilder` склеивает их по `index`. Если апстрим не прислал `id`,
  подставляется `call_<n>`. Аргументы-объект (а не строка) превращаются в JSON-строку.
- Если апстрим проигнорировал `stream: true` и ответил обычным JSON — он тоже разбирается.
- `data: {"error": …}` внутри потока и любой код ≠ 200 → `LLMError(status, message)`;
  сетевые ошибки → `LLMError(502, …)`.

`models()` — список id из `GET /models` (CLI берёт первую модель, если она не задана).
