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

## Завершение ответа и возможности модели

Ограниченный парсер `agent/streaming.py` поддерживает CRLF, многострочные data и последний
блок без пустой строки; предел события 1 MiB. Отсутствие finish_reason и [DONE] — ошибка
обрыва, а не пустой успешный ответ. length/content_filter в агенте завершают ход ошибкой;
неполные вызовы не исполняются. Для вызовов требуется finish_reason=tool_calls и корректные
структурные id/type/name/arguments. Ошибки конкретного JSON аргументов по-прежнему уходят
модели как результат инструмента для исправления.

Некорректные JSON, типы и структура ответа превращаются в LLMError. Обычный JSON-ответ
поддерживается при stream=false или игнорировании потока сервером. Параметры tools, stream,
temperature, usage и имя поля max_tokens управляются возможностями подключения и model_profiles.
При несовместимости инструментов запрос не посылается. Повторы действий автоматически не делаются.
Общий ModelLimiter может оборачивать весь запрос, включая чтение ответа; вложенный генератор
закрывается до освобождения слота, в том числе при отмене.

## Reasoning и заголовки корреляции

Клиент принимает строковые `reasoning_content` и `reasoning` в JSON-message и SSE-delta.
Возвращает отдельные дельты `reasoning`; итог хранится в `reasoning_content` сообщения
assistant, включая историю для следующего запроса. Если провайдер не отдаёт reasoning,
блок рассуждений не появляется. Содержимое ответов не записывается в журнал метаданных.
Из текущего контекста `agent/tracing.py` передаются `X-Request-ID`, `X-Span-ID`, `X-Parent-Span-ID`.
Каждый вызов модели имеет свой span; root request остаётся общим для дочерних задач.


Параметры профиля temperature_value, top_p, reasoning_effort попадают в реальное тело
chat/completions; api_model заменяет ID на стороне API, сохраняя локальное имя сессии.
Ключевые model/messages/stream нельзя заменить произвольным JSON. Модельные лимиты ответа
применяются через штатный max_tokens/max_completion_tokens. [Обмен OpenCode](opencode.md).

LLMError содержит retryable и retry_after, сообщения ошибок приводятся к ограниченной строке. HTTP Retry-After разбирается в секундах или HTTP-дате; SSE error.code/status распознаёт временные ошибки. Сам LLMClient запросы не повторяет: повтором и отменой управляет Agent. Ошибки формата не помечаются временными. [Повторы](request-processing.md).

Суммаризация использует копию клиента с тем же подключением и семафором, без инструментов, с отдельным лимитом выхода. Основные options/max_tokens не меняются. model.start различает purpose=response/compaction.

LLMClient.models по-прежнему возвращает список ID, дополнительно заполняет model_metadata числовыми лимитами и aliases. discover_model_limits читает каталог один раз для клиента с таймаутом 5 секунд; ошибки каталога не блокируют chat/completions. Новый клиент имеет отдельный каталог.
