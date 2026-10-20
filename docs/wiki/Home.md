# LLM Gateway — вики

Шлюз к инференсу за mTLS с личными токенами и **агентом**: модель вызывает инструменты (файлы,
shell, сеть, MCP), права и хуки решают, что можно, сессии хранятся в SQLite, настройки — в JSON.
Без фреймворков агентов: stdlib + уже используемые шлюзом `httpx`/FastAPI.

Краткое описание шлюза и установка — [README](../../README.md).

## Устройство
- [architecture.md](architecture.md) — компоненты, как запрос проходит через систему
- [agent-loop.md](agent-loop.md) — цикл агента и его события
- [llm-client.md](llm-client.md) — клиент chat/completions, стрим и сборка tool_calls

## Настройка
- [providers.md](providers.md) — именованные подключения и выбор модели для CLI и бенчмарков
- [settings.md](settings.md) — все ключи JSON-настроек, слои CLI, профили сервера
- [permissions.md](permissions.md) — правила allow/ask/deny, режимы, подтверждения
- [hooks.md](hooks.md) — хуки на события агента
- [mcp.md](mcp.md) — подключение MCP-серверов (stdio, Streamable HTTP)
- [sessions.md](sessions.md) — хранение сессий в SQLite

## Инструменты
- [tools/files.md](tools/files.md) — `read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`
- [tools/question.md](tools/question.md) — `question`, уточняющие вопросы пользователя
- [tools/planning.md](tools/planning.md) — `goal`, `todo`, цель и задачи сессии
- [tools/shell.md](tools/shell.md) — `shell`
- [tools/web.md](tools/web.md) — `web_fetch`, `http_request`, `web_search`, защита от SSRF

## Интерфейсы
- [server-api.md](server-api.md) — агент на сервере: `/agent/*`, поток событий SSE
- [cli.md](cli.md) — терминальный агент `gwagent`
- [web-chat.md](web-chat.md) — веб-чат: режимы «Чат» и «Агент»

## Эксплуатация
- [technical-debt.md](technical-debt.md) — результат аудита, выполненные задачи и ограничения
- [bench/README.md](bench/README.md) — бенчмарки качества, скорости и контекстного окна
- [security.md](security.md) — модель угроз и рекомендации
- [testing.md](testing.md) — как устроены тесты, как добавлять новые
- [changelog.md](changelog.md) — история изменений

## Правило для вики
Правка модуля = правка его страницы в том же коммите + строка в [changelog.md](changelog.md).
[`tests/test_wiki.py`](../../tests/test_wiki.py) падает, если инструмент, ключ настроек, событие хука или цикла,
эндпоинт `/agent/*` или страница вики не описаны, а также при битых относительных ссылках.

- [CLI на OpenTUI: таблицы и важные замечания](tui.md)

- [Сабагент](tools/subagent.md) — изолированные дочерние задачи.
