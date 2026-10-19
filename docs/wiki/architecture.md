# Архитектура

```
                      ┌──────────────── gateway/ (FastAPI, один процесс uvicorn :7700) ─────────────────┐
 openai SDK, cliAgent ─┤ /v1/*  прокси байт-в-байт ─────────────────────────────┐                        │
                      │                                                         ├─ mTLS-клиент httpx ───┼─▶ инференс
 веб-чат /chat/ ───────┤ /agent/* agent_api.py ─▶ agent/ (ядро) ─ LLMClient ────┘  (сертификат шлюза)   │
                      │                          │  инструменты на сервере                              │
                      │ tokens.py: токены gw-…, usage  ·  data/gateway.db: токены + сессии агента       │
                      └─────────────────────────────────────────────────────────────────────────────────┘

 gwagent (любая машина) ─ agent/ (то же ядро) ─ LLMClient ─Bearer gw-…─▶ шлюз /v1/chat/completions
                          │ инструменты, MCP, хуки — локально; сессии — ~/.local/share/gwagent/sessions.db
```

## Пакеты

| Путь | Что | Зависит от |
|---|---|---|
| `gateway/` | шлюз: конфиг из `.env`, токены, прокси `/v1`, API агента | FastAPI, httpx, `agent/` |
| `agent/` | ядро агента, **без FastAPI** | stdlib, httpx |
| `agent/tools/` | встроенные инструменты | stdlib, httpx |
| `chat/` | веб-чат, статика | — |
| `gwtoken`, `gwagent` | CLI (перезапускаются из `venv/`) | `gateway/`, `agent/` |

## Ядро `agent/`

| Модуль | Роль | Страница |
|---|---|---|
| `settings.py` | JSON-настройки: умолчания, слои, профили, валидация | [settings.md](settings.md) |
| `schema.py` | мини-валидатор JSON Schema (настройки + аргументы инструментов) | [settings.md](settings.md) |
| `store.py` | сессии, сообщения, журнал вызовов в SQLite | [sessions.md](sessions.md) |
| `llm.py` | `LLMClient`: стрим chat/completions, сборка tool_calls | [llm-client.md](llm-client.md) |
| `loop.py` | `Agent`: цикл и события | [agent-loop.md](agent-loop.md) |
| `permissions.py` | allow/ask/deny | [permissions.md](permissions.md) |
| `hooks.py` | хуки-команды | [hooks.md](hooks.md) |
| `mcp.py` | MCP-клиент и обёртки инструментов | [mcp.md](mcp.md) |
| `tools/base.py` | `Tool`, `Registry`, `ToolContext`, `ToolError` | ниже |
| `tools/files.py`, `shell.py`, `web.py` | встроенные инструменты | [tools/files.md](tools/files.md), [tools/shell.md](tools/shell.md), [tools/web.md](tools/web.md) |
| `runtime.py` | сборка `Agent` по настройкам (реестр, MCP, хуки) | — |
| `cli.py` | `gwagent` | [cli.md](cli.md) |

## Путь одного шага агента

1. Фронт (CLI / `/agent/sessions/{id}/messages`) создаёт `Agent` через `runtime.make_agent`.
2. `Agent.run` → хуки → `LLMClient.stream` с историей из `SessionStore` и `Registry.specs()`.
3. Модель вернула `tool_calls` → для каждого: `parse_arguments` → `Tool.check_args` (схема) →
   `PreToolUse` → `Permissions.decide` → (`approver`) → `Tool.run(args, ToolContext)` → `PostToolUse`.
4. Всё сохраняется в SQLite сразу; события уходят фронту (печать / SSE).

## Свой инструмент

```python
from agent.tools.base import Tool, ToolError

class Weather(Tool):
    name = "weather"
    description = "Погода в городе"
    parameters = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    default_permission = "allow"              # или "ask"

    def subject(self, args):                  # для правил weather(Моск*)
        return args["city"]

    async def run(self, args, ctx):           # ctx.workspace, ctx.settings, ctx.session_id
        if not args["city"]:
            raise ToolError("пустой город")   # уйдёт модели как ошибка
        return "солнечно"                     # str или ToolResult(content, is_error)
```

Затем добавить класс в `TOOLS` своего модуля и в `BUILTIN` (`agent/runtime.py`), имя — в
`DEFAULTS["tools"]["enabled"]`, тесты — `tests/test_tools_<модуль>.py`, страницу — `docs/wiki/tools/`.
`tests/test_wiki.py` напомнит, если страница забыта.

## Решения

- **Без фреймворков**: протоколы (OpenAI chat/completions, MCP JSON-RPC, SSE) реализованы напрямую —
  это несколько сотен строк, зато видно и тестируемо каждое поведение.
- **Сервер зовёт инференс напрямую**, а не через свой `/v1`: нет лишнего HTTP-круга и второй
  аутентификации; расход всё равно пишется в `usage` (путь `agent`).
- **Вызовы инструментов — последовательно**, история пишется после каждого шага: отмена или
  падение процесса не ломают сессию.
- **Системный промпт не хранится** — собирается из текущих настроек.
