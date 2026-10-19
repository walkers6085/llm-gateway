# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это
OpenAI-совместимый прокси (FastAPI + httpx) к своему инференсу, доступному только по клиентскому сертификату (mTLS), плюс **агент** поверх него. Сертификат и `UPSTREAM_API_KEY` живут только на шлюзе; пользователи ходят по личным токенам `gw-…`. Документация, сообщения об ошибках, комментарии и коммиты — на русском.

Ограничение владельца: **без фреймворков агентов и больших библиотек** — только stdlib + уже используемые `httpx`/FastAPI. Протоколы (chat/completions, SSE, MCP JSON-RPC, JSON Schema-валидация) реализованы вручную.

## Вики — обязательна
Подробная документация — `docs/wiki/` (оглавление `Home.md`). **Правка модуля = правка его страницы вики + строка в `docs/wiki/changelog.md` в том же коммите.** Карта «модуль → тесты → страница» — `docs/wiki/testing.md`. `tests/test_wiki.py` падает, если новый инструмент, ключ `DEFAULTS`, событие хука/цикла, эндпоинт `/agent/*` или страница не описаны, а также при битых относительных ссылках.

## Команды
```bash
venv/bin/pytest -q                                   # все тесты (~20 с, офлайн; поднимают настоящий mTLS-апстрим)
venv/bin/pytest -q tests/test_loop.py::test_cancel_keeps_history_valid   # один тест
venv/bin/uvicorn --factory gateway.app:create_app --host 0.0.0.0 --port 7700   # ручной запуск шлюза
./gwtoken create|list|revoke|usage|agent             # токены; `agent <имя> <профиль>|--off` — доступ к /agent/*
GW_TOKEN=gw-… ./gwagent [-p "задача"]                # CLI-агент; оба скрипта перезапускаются из venv/
```
- Продакшен — user-юнит `~/.config/systemd/user/llm-gateway.service` (вне репозитория: `EnvironmentFile=.env`, порт `${PORT}`, `MemoryMax=300M`). После правок Python и `config/agent.json` — `systemctl --user restart llm-gateway`; `chat/` — статика. Логи: `journalctl --user -u llm-gateway -f`.
- С 2026-10-16 сервис **остановлен и disabled** — ждёт реальных `UPSTREAM_BASE_URL` и сертификатов в `certs/`. Не включать без запроса владельца.
- Линтера/форматтера нет. Мало RAM — тесты не запускать параллельно с тяжёлыми задачами.

## Архитектура
Два пакета: `gateway/` (FastAPI) и `agent/` (ядро агента, **не импортирует FastAPI**). Подробно — `docs/wiki/architecture.md`.

**Шлюз**
- `gateway/config.py` — `Settings.from_env()` читает `.env` из корня (load_dotenv при импорте, существующие переменные окружения главнее). Относительные пути резолвятся от корня. `AGENT_CONFIG` — профили агента (`config/agent.json`, gitignored).
- `gateway/tokens.py` — `TokenStore` (SQLite `data/gateway.db`, WAL): `tokens` (sha256, префикс, срок, allowlist моделей, rpm, `agent_profile` — добавляется миграцией `ALTER TABLE` для старых баз) и `usage` (агентские обращения к модели — с путём `agent`).
- `gateway/app.py` — всё внутри фабрики `create_app(settings, store, transport)` (замыкания; uvicorn с `--factory`). Ошибка сертификата/пустой URL не роняют процесс → `app.state.config_error`, `/v1/*` и `/agent/*/messages` отвечают 503 `gateway_not_configured`. httpx-клиент с `trust_env=False` (системные прокси намеренно игнорируются). В `POST /v1/{path}` семафор `ModelLimiter` освобождается и расход пишется в `finish()` на всех путях — сохранять этот инвариант.
- `gateway/agent_api.py` — `AgentService` + `mount()`: `/agent/*` с SSE. Переиспользует `authenticate`, `upstream()`, `upstream_headers()` и `GatewayError` из `create_app`; модель зовётся напрямую mTLS-клиентом (`app.state.client`), не через `/v1`. Идущий запрос — `Run` (задача + очередь событий + futures подтверждений); разрыв SSE отменяет задачу. MCP-менеджеры — по одному на профиль, закрываются в lifespan.

**Ядро `agent/`**
- `loop.py` — `Agent.run(text)` — асинхронный генератор событий-словарей (их одинаково потребляют CLI, SSE и тесты). Вызовы инструментов последовательны; каждое сообщение сразу пишется в SQLite. Инвариант: на каждый `tool_calls[i].id` в истории есть `tool`-сообщение (при отмене — «Отменено пользователем»), иначе апстрим отвергнет историю.
- `runtime.py` — сборка `Agent` по настройкам (`BUILTIN`-реестр инструментов, MCP, хуки); единая точка для CLI и сервера.
- `settings.py` — `DEFAULTS` + `SCHEMA` (валидатор `schema.py`, он же проверяет аргументы инструментов). Новый ключ = `DEFAULTS` + `SCHEMA` + строка в `docs/wiki/settings.md`.
- `permissions.py` — порядок: deny-правила → решение хука `PreToolUse` → allow (+ правила «всегда» сессии) → ask → режим/умолчание инструмента (`Tool.default_permission`). Шаблон правила сравнивается с `Tool.subject(args)`.
- `hooks.py` — внешние команды: JSON на stdin, код 2 = блок, stdout-JSON = решение/`updated_input`/`additional_context`.
- `mcp.py` — свой клиент MCP (stdio и Streamable HTTP); инструменты — `mcp__<сервер>__<имя>`.
- `tools/` — `files.py` (все пути через `resolve()` внутри workspace), `shell.py` (не песочница: урезанное окружение, группа процессов, таймаут), `web.py` (SSRF-проверка каждого хоста и редиректа).
- `store.py` — сессии в SQLite; версия схемы в таблице `agent_meta`, **не** в `PRAGMA user_version` (база на сервере общая с токенами).
- `cli.py` — `gwagent`.

`chat/index.html` — одностраничный чат без сборки, режимы «Чат» (`/v1`) и «Агент» (`/agent/*`).

## Тесты
- Общие подделки — `tests/fakes.py`: PKI, mTLS-апстрим (`upstream`) со сценарием `SCRIPT` (ответы ассистента с `tool_calls`, отдаются SSE-потоком кусками), `SEEN`, `ServerThread`, `scripted_transport` (MockTransport для цикла без сети). Фикстуры — `tests/conftest.py` (`make(**поля Settings)` → `(TestClient, TokenStore)`, чистит `SEEN`/`SCRIPT`).
- `async def test_*` запускает хук в `conftest.py` (pytest-asyncio не нужен).
- Всё офлайн: сайты, поисковик, MCP-серверы — локальные (`tests/fixtures/`). Без моков TLS.
