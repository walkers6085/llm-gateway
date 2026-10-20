# AGENTS.md

## Что это
OpenAI-совместимый прокси (FastAPI + httpx) к инференсу за mTLS + агент поверх него. Полная инструкция — `CLAUDE.md`, подробная документация — `docs/wiki/Home.md` (оглавление).

## Жёсткие ограничения
- **Без фреймворков агентов и больших библиотек** — только stdlib + уже используемые `httpx`/FastAPI.
- Документация, сообщения об ошибках, комментарии, коммиты — **на русском**.
- Сертификаты (`certs/`) и `UPSTREAM_API_KEY` живут только на шлюзе; пользователи ходят токенами `gw-…`.

## Вики — обязательна
**Правка модуля = правка его страницы вики + строка в `docs/wiki/changelog.md` в том же коммите.** `tests/test_wiki.py` падает, если новый инструмент, ключ `DEFAULTS`, событие хука/цикла, эндпоинт `/agent/*` или страница не описаны, а также при битых относительных ссылках. Карта «модуль → тесты → страница» — `docs/wiki/testing.md`.

## Команды
```bash
venv/bin/pytest -q                                                  # всё (≈20 с), офлайн
venv/bin/pytest -q tests/test_loop.py::test_cancel_keeps_history_valid   # один тест
venv/bin/uvicorn --factory gateway.app:create_app --host 0.0.0.0 --port 7700
./gwtoken create|list|revoke|usage|agent        # токены; agent <имя> <профиль>|--off
GW_TOKEN=gw-… ./gwagent [-p "задача"]           # CLI-агент; оба скрипта сами перезапускаются из venv/
node --test tests/test_chat.mjs                 # тест веб-чата (Node, не pytest)
bun run --cwd tui typecheck && bun run --cwd tui test   # TUI (нужен Bun ≥ 1.3.8)
python -m pip wheel --no-deps --wheel-dir /tmp/got-w .   # проверка упаковки
```
- Установка dev-зависимостей: `pip install -r requirements-dev.txt -c requirements.lock` (runtime — `requirements.txt`. pytest отделён). CI (`.github/workflows/tests.yml`): bun typecheck+test → pytest → chat-node → wheel → `git diff --exit-code`.
- Пакет: `pyproject.toml`, Python ≥ 3.12; CLI-точки `gwagent`/`gwproviders`/`gwbench`.

## Архитектура (неочевидное)
- Два пакета: `gateway/` (FastAPI, всё внутри фабрики `create_app` — замыкания, uvicorn с `--factory`) и `agent/` (ядро агента, **не импортирует FastAPI**). Ещё `bench/`, `tui/` (TypeScript/OpenTUI, собирается не нужен), `chat/` (статика).
- `gateway/app.py`: ошибка сертификата/пустой URL не роняют процесс → `app.state.config_error`, `/v1/*` и `/agent/*/messages` отвечают 503 `gateway_not_configured`. В `POST /v1/{path}` семафор `ModelLimiter` освобождается и расход пишется в `finish()` на всех путях — **сохранять инвариант**.
- `gateway/agent_api.py` (`AgentService`): модель зовётся напрямую mTLS-клиентом (`app.state.client`), **не через `/v1`**; MCP-менеджеры — по одному на профиль, закрываются в lifespan.
- `agent/loop.py`: `Agent.run(text)` — асинхронный генератор событий-словарей (одинаково потребляют CLI, SSE, тесты). **Инвариант истории:** на каждый `tool_calls[i].id` есть `tool`-сообщение (при отмене — «Отменено пользователем»), иначе апстрим отвергнет историю.
- `agent/settings.py`: новый ключ = `DEFAULTS` + `SCHEMA` + строка в `docs/wiki/settings.md` (валидатор `schema.py`).
- `agent/runtime.py` — единая точка сборки агента для CLI и сервера; `permissions.py` deny → хук `PreToolUse` → allow → ask → умолчание инструмента.
- SQLite: схема версии в таблице `agent_meta`, **не** в `PRAGMA user_version` (база на сервере общая с токенами). Все операции соединения — под RLock; хранилища закрываются в lifecycle.
- Файловые пути инструментов — через `resolve()` внутри workspace; shell — не песочница (урезанное окружение, группа процессов, таймаут); web — SSRF-проверка каждого хоста и редиректа.
- Отмена: закрывает вложенные генераторы и подпроцессы; общий стрим — `agent/streaming.py`.
- Проектный `.gwagent/settings.json` требует `gwagent trust` до применения. В тестах пользовательские конфиги/реестр доверия изолируются autouse-фикстурой — не писать в реальные `~/.config`. Секреты — `agent/credentials.py` + отдельный `auth.json` (0600).

## Тесты (всё офлайн)
- Подделки — `tests/fakes.py` (PKI, mTLS-апстрим `upstream` со сценарием `SCRIPT`, SSE-поток кусками, `ServerThread`, `scripted_transport`); фикстура `make(**поля Settings)` в `tests/conftest.py`.
- `async def test_*` запускает хук в `conftest.py` — pytest-asyncio не нужен.
- Сайты, поисковик, MCP-серверы — локальные (`tests/fixtures/`), без моков TLS; тесты поднимают настоящий mTLS-апстрим с `CERT_REQUIRED`.

## Эксплуатация
- Продакшен — user-юнит `~/.config/systemd/user/llm-gateway.service` (вне репо). После правок Python и `config/agent.json` — `systemctl --user restart llm-gateway`; `chat/` — статика. Логи: `journalctl --user -u llm-gateway -f`.
- **С 2026-10-16 сервис остановлен и disabled** — ждёт реальных `UPSTREAM_BASE_URL` и сертификатов в `certs/`. Не включать без запроса владельца.
- Линтера/форматтера нет. Мало RAM — не запускать тесты параллельно с тяжёлыми задачами.
- Пакетные CLI-установки после правок вики — нет; карта оставшегося техдолга — `docs/wiki/technical-debt.md`.
