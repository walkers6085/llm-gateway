# MCP-серверы

Код: [`agent/mcp.py`](../../agent/mcp.py), подключение к агенту — [`agent/runtime.py`](../../agent/runtime.py).
Тесты: [`tests/test_mcp.py`](../../tests/test_mcp.py), игрушечный сервер — [`tests/fixtures/mcp_stdio_server.py`](../../tests/fixtures/mcp_stdio_server.py).

Свой клиент [Model Context Protocol](https://modelcontextprotocol.io) (JSON-RPC 2.0) без SDK.
Поддерживаются **инструменты** (`tools/list`, `tools/call`); ресурсы, промпты и sampling — нет.

## Настройка

```json
"mcp": {
  "timeout": 60,
  "servers": {
    "fs":     { "type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv/data"] },
    "github": { "type": "http", "url": "https://api.githubcopilot.com/mcp/",
                "headers": { "Authorization": "Bearer ${GITHUB_TOKEN}" } },
    "test":   { "type": "stdio", "command": "python3", "args": ["tests/fixtures/mcp_stdio_server.py"], "enabled": false }
  }
}
```

| Поле | Для | Описание |
|---|---|---|
| `type` | все | `stdio` или `http` (обязательно) |
| `command`, `args` | stdio | что запустить |
| `env` | stdio | переменные поверх окружения процесса |
| `cwd` | stdio | рабочая папка сервера |
| `url` | http | адрес эндпоинта Streamable HTTP |
| `headers` | http | заголовки каждого запроса (токены) |
| `timeout` | все | таймаут запроса, с (по умолчанию `mcp.timeout`) |
| `enabled` | все | `false` — не запускать |

В `command`, `args`, `env`, `cwd`, `url`, `headers` подставляются переменные окружения `${VAR}` / `$VAR` —
секреты не нужно писать в файл. Значения заголовков — только ASCII (ограничение HTTP).

## Как работает

1. При старте (CLI — при запуске, сервер — при первом обращении к профилю) все включённые серверы
   поднимаются параллельно: `initialize` → `notifications/initialized` → `tools/list` (со всеми
   страницами `nextCursor`). Сервер, который не поднялся, пишется в ошибки (`/mcp` в CLI,
   `GET /agent/tools` на сервере) и не мешает остальным.
2. Каждый инструмент регистрируется как `mcp__<сервер>__<инструмент>` (символы кроме `A-Za-z0-9_-`
   заменяются на `_`, имя обрезается до 64), описание — `[MCP <сервер>] …`, схема — `inputSchema`
   сервера как есть. Встроенный инструмент с тем же именем важнее.
3. Аргументы MCP-инструментов агент **не** проверяет своим валидатором (схемы серверов бывают
   сложнее) — проверяет сервер.
4. Результат: `text` — как есть; `image`/`audio` — `[image image/png, N байт]`; `resource` — его текст
   или URI; `resource_link` — URI; пусто, но есть `structuredContent` — он в JSON. `isError: true` →
   результат с ошибкой.

Права по умолчанию для всех MCP-инструментов — **ask**. Разрешить сервер целиком: `"allow": ["mcp__fs__*"]`.

### stdio

- Подпроцесс в своей группе, одна JSON-строка на сообщение (до 16 МБ), stderr сервера копится
  (последние 20 строк) и показывается, если сервер умер.
- Запросы сервера к клиенту: на `ping` отвечаем `{}`, на остальное — ошибка `-32601`; уведомления
  игнорируются.
- Упал — при следующем вызове один автоматический перезапуск (с `initialize`).
- Закрытие: закрываем stdin, ждём 2 с, затем `SIGKILL` группе.

### Streamable HTTP

- Каждое сообщение — `POST` с `Accept: application/json, text/event-stream`. Ответ — JSON или SSE
  (из потока берётся событие с нашим `id`, уведомления пропускаются).
- `Mcp-Session-Id` из ответа на `initialize` отправляется дальше; после инициализации — заголовок
  `MCP-Protocol-Version`. 404 при живой сессии = сессия истекла (ошибка вызова).
- При закрытии — `DELETE` с id сессии. Прокси — `network.proxy`, системные прокси не используются.
- Запросы сервера к клиенту внутри SSE (sampling, elicitation) не поддерживаются.

## Проверка руками

```bash
cat > /tmp/mcp.json <<'J'
{"mcp": {"servers": {"test": {"type": "stdio", "command": "python3",
  "args": ["/home/deb/llm-gateway/tests/fixtures/mcp_stdio_server.py"]}}}}
J
./gwagent --settings /tmp/mcp.json     # затем /mcp — список серверов и инструментов
```
