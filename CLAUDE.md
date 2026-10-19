# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это
OpenAI-совместимый прокси (FastAPI + httpx) к своему инференсу, доступному только по клиентскому сертификату (mTLS). Сертификат и `UPSTREAM_API_KEY` живут только на шлюзе; пользователи ходят по личным токенам `gw-…`. Протоколы не транслируются — ответы апстрима отдаются как есть, SSE-стрим проксируется байт-в-байт. Документация, сообщения об ошибках, комментарии и коммиты — на русском.

## Команды
```bash
venv/bin/pytest -q                                   # все тесты (поднимают настоящий mTLS-апстрим)
venv/bin/pytest -q tests/test_gateway.py::test_stream  # один тест
venv/bin/uvicorn --factory gateway.app:create_app --host 0.0.0.0 --port 7700   # ручной запуск
./gwtoken create|list|revoke|usage                   # токены; сам перезапускается из venv/
```
- Продакшен — user-юнит `~/.config/systemd/user/llm-gateway.service` (вне репозитория: `EnvironmentFile=.env`, порт `${PORT}`, `MemoryMax=300M`). После правок Python — `systemctl --user restart llm-gateway`; `chat/` — статика, перезапуск не нужен. Логи: `journalctl --user -u llm-gateway -f`.
- С 2026-10-16 сервис **остановлен и disabled** — ждёт реальных `UPSTREAM_BASE_URL` и сертификатов в `certs/`. Не включать без запроса владельца.
- Линтера/форматтера в проекте нет.

## Архитектура
- `gateway/config.py` — `Settings.from_env()` читает `.env` из корня проекта (load_dotenv при импорте). Относительные пути (`CLIENT_CERT`, `DB_PATH`, `CA_BUNDLE` — несколько через запятую) резолвятся от корня. `MODEL_MAX_CONNECTIONS` и `CORS_ALLOW_ORIGINS` — JSON. `GATEWAY_MODE` по умолчанию `PROD` (без `/docs`).
- `gateway/tokens.py` — `TokenStore` на SQLite (`data/gateway.db`, WAL, autocommit): таблица `tokens` (только sha256 + префикс `raw[:10]`, срок, `models` через запятую, `rpm`) и `usage` (по запросу: путь, модель, статус, токены, мс; тексты не пишутся). `find()` ищет по имени или префиксу и возвращает `None` при неоднозначности.
- `gateway/app.py` — вся логика внутри фабрики `create_app(settings, store, transport)` (замыкания, не глобалы); uvicorn запускается с `--factory`.
  - `lifespan` строит `ssl.SSLContext` (CA + клиентский cert/key). Ошибка загрузки сертификата или пустой URL **не роняют процесс** — пишутся в `app.state.config_error`, и все `/v1/*` отвечают 503 `gateway_not_configured` (чтобы systemd не уходил в цикл рестартов). `httpx.AsyncClient` создаётся с `trust_env=False`: системные `HTTP(S)_PROXY` намеренно игнорируются, прокси — только явный `UPSTREAM_PROXY`.
  - `authenticate` (Depends): `Authorization: Bearer` или `x-api-key` → lookup → проверка активности → `RateLimiter` (скользящее окно 60 с в памяти процесса).
  - `POST /v1/{path}` проксирует только `PROXIED_POST` (`chat/completions`, `completions`, `embeddings`); проверяет allowlist моделей токена, лимит тела, берёт семафор `ModelLimiter` на модель. Для stream-чата принудительно добавляет `stream_options.include_usage`, а `relay()` выуживает `usage` из SSE-строк, не меняя поток. Семафор освобождается и расход пишется в `finish()` — на всех путях (ошибка соединения, не-SSE ответ, обрыв стрима, 499 при отмене клиентом). При правках сохранять этот инвариант.
  - `GET /v1/models` фильтрует список по allowlist токена. `/health` дёргает апстримный `/models`.
  - Ошибки — `GatewayError` → тело в формате OpenAI (`{"error": {message, type, code, param}}`); ошибки апстрима классифицируются в `upstream_failure` (504 timeout / 502 TLS / 502 unavailable), полная цепочка причин — в лог через `describe()`.
- `chat/index.html` — одностраничный чат без сборки, монтируется на `/chat` (`/` редиректит туда); токен хранит в `localStorage`, ходит в `/v1/models` и `/v1/chat/completions` того же origin (поэтому CORS по умолчанию выключен).

## Тесты
`tests/test_gateway.py` генерирует PKI через `cryptography` (CA, серверный и клиентский сертификаты), поднимает в потоке uvicorn-апстрим с `ssl_cert_reqs=CERT_REQUIRED` (фикстура `upstream_url`, scope session) и создаёт шлюз через фикстуру `make(**overrides)`, которая переопределяет поля `Settings`. Апстрим складывает полученные запросы в `SEEN` (чистится в `make`). Новые тесты — по этому образцу, без моков TLS.
