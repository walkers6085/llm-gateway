# Тесты

```bash
venv/bin/pytest -q                              # всё (≈20 с)
venv/bin/pytest -q tests/test_loop.py           # один модуль
venv/bin/pytest -q tests/test_cli.py::test_repl_approvals_commands_and_resume
```

Всё офлайн: интернет не нужен, внешние сервисы заменены локальными. На сервере мало RAM —
тесты и тяжёлые сборки параллельно не запускать.

## Устройство

| Файл | Что внутри |
|---|---|
| [`tests/fakes.py`](../../tests/fakes.py) | PKI (`make_pki`), фейковый апстрим (`upstream`, `SEEN`, `SCRIPT`), `ServerThread`, `scripted_transport`, `tool_call`, `auth` |
| [`tests/conftest.py`](../../tests/conftest.py) | запуск `async def test_*` (без pytest-asyncio), фикстуры `pki`, `upstream_url`, `gateway_settings`, `make` |
| [`tests/fixtures/`](../../tests/fixtures/) | игрушечный MCP-сервер (stdio), HTML выдачи DuckDuckGo |

- **Апстрим** — настоящий uvicorn с TLS и `CERT_REQUIRED`: шлюз обязан предъявить клиентский
  сертификат. Без сценария отвечает «привет»; со сценарием `SCRIPT` — по очереди отдаёт заданные
  ответы ассистента потоком SSE, разбивая аргументы `tool_calls` на куски (как настоящие модели).
- **Цикл агента** в `test_loop.py` идёт без сети: `LLMClient` поверх `httpx.MockTransport`
  (`scripted_transport`) и игрушечные инструменты.
- **Сервер** (`test_agent_api.py`) — `TestClient` шлюза + mTLS-апстрим; подтверждения проверяются
  параллельным потоком, который читает SSE, пока основной шлёт `approve`.
- **CLI** (`test_cli.py`) — шлюз в `ServerThread`, `amain()` с подменёнными вводом/выводом.
- **Сеть** (`test_tools_web.py`) — локальный FastAPI-сайт; проверки SSRF — на адресах, которые не
  требуют сети (`10.0.0.1`, `169.254.169.254`, IPv4-mapped).
- **MCP** (`test_mcp.py`) — `fixtures/mcp_stdio_server.py` (пагинация, уведомления, ping от сервера,
  падение, таймаут) и FastAPI-сервер Streamable HTTP (сессия, SSE-ответ, DELETE).
- **Вики** (`test_wiki.py`) — см. [Home.md](Home.md#правило-для-вики).

## Карта: модуль → тесты → страница

| Модуль | Тесты | Вики |
|---|---|---|
| `agent/settings.py`, `schema.py` | `test_settings.py` | [settings.md](settings.md) |
| `agent/store.py` | `test_store.py` | [sessions.md](sessions.md) |
| `agent/llm.py` | `test_llm.py` | [llm-client.md](llm-client.md) |
| `agent/loop.py` | `test_loop.py` | [agent-loop.md](agent-loop.md) |
| `agent/permissions.py` | `test_permissions.py`, `test_loop.py` | [permissions.md](permissions.md) |
| `agent/hooks.py` | `test_hooks.py`, `test_loop.py` | [hooks.md](hooks.md) |
| `agent/tools/files.py` | `test_tools_files.py` | [tools/files.md](tools/files.md) |
| `agent/tools/shell.py` | `test_tools_shell.py` | [tools/shell.md](tools/shell.md) |
| `agent/tools/web.py` | `test_tools_web.py` | [tools/web.md](tools/web.md) |
| `agent/mcp.py` | `test_mcp.py`, `test_cli.py` | [mcp.md](mcp.md) |
| `gateway/agent_api.py` | `test_agent_api.py` | [server-api.md](server-api.md) |
| `agent/cli.py` | `test_cli.py` | [cli.md](cli.md) |
| `chat/index.html` | `test_agent_api.py` (страница), ручной прогон | [web-chat.md](web-chat.md) |
| `gateway/app.py` (прокси) | `test_gateway.py` | [README](../../README.md) |

## Сценарий для нового теста агента

```python
from tests.fakes import SCRIPT, tool_call

SCRIPT.extend([
    {"content": "Читаю", "tool_calls": [tool_call("c1", "read_file", path="a.txt")]},
    {"content": "Готово"},                      # финальный ответ без инструментов
    # {"status": 503, "error": "перегружен"},  # ошибка апстрима
])
```

Фикстура `make` очищает `SEEN` и `SCRIPT` до и после теста; в `SEEN` видно, что ушло апстриму
(тело запроса, заголовок авторизации).
