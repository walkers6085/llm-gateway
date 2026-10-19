# История изменений

Новые записи — сверху. Правка модуля = строка здесь + правка его страницы вики.

## 2026-10-19 — бенчмарки и провайдеры

- Отдельный CLI `python -m bench`: скорость, принимаемое контекстное окно, иголка в стоге сена,
  арифметика, инструменты, JSON по схеме и цепочки действий; отчёты JSON/Markdown,
  сравнение запусков, офлайн-тесты ([bench/README.md](bench/README.md)).
- Именованные OpenAI-совместимые подключения, ключи из окружения, управление и каталог моделей;
  выбор провайдера в бенчмарках и CLI агента, сохранение привязки сессий
  ([providers.md](providers.md), [cli.md](cli.md), [sessions.md](sessions.md)).

## 2026-10-19 — агент

- Ядро `agent/`: цикл агента с событиями, клиент chat/completions со сборкой `tool_calls`
  ([agent-loop.md](agent-loop.md), [llm-client.md](llm-client.md)).
- Настройки в JSON: слои CLI, профили сервера в `config/agent.json`, валидация с путём ключа ([settings.md](settings.md)).
- Сессии в SQLite: история, правила «всегда», журнал вызовов, миграции ([sessions.md](sessions.md)).
- Инструменты: файлы в пределах рабочей папки, `shell`, `web_fetch`/`http_request`/`web_search`
  с защитой от SSRF ([tools/files.md](tools/files.md), [tools/shell.md](tools/shell.md), [tools/web.md](tools/web.md)).
- Права allow/ask/deny, режимы `allow_all`/`read_only`, подтверждения ([permissions.md](permissions.md)).
- Хуки-команды на 5 событий ([hooks.md](hooks.md)), пример политики `config/hooks/check_shell.py`.
- Клиент MCP: stdio и Streamable HTTP ([mcp.md](mcp.md)).
- Шлюз: API `/agent/*` с SSE, колонка `tokens.agent_profile`, `gwtoken create --agent`,
  `gwtoken agent`, переменная `AGENT_CONFIG` ([server-api.md](server-api.md)).
- CLI `gwagent` ([cli.md](cli.md)), режим «Агент» в веб-чате ([web-chat.md](web-chat.md)).
- Тесты вынесены в общие фикстуры (`tests/conftest.py`, `tests/fakes.py`), добавлен `tests/test_wiki.py`.

## 2026-10-14 — шлюз

- mTLS-прокси `/v1/*`, токены `gw-…` с учётом, веб-чат ([README](../../README.md)).
