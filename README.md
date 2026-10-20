# LLM Gateway

OpenAI-совместимый шлюз к инференсу, который доступен только по клиентскому сертификату (mTLS).
Сертификат лежит **только на шлюзе**, пользователи ходят по личным токенам `gw-…`.
Идея как у [gpt2giga](https://github.com/ai-forever/gpt2giga), но без трансляции протоколов
(апстрим уже говорит на OpenAI API) и с пользовательскими токенами вместо одного общего ключа.

```
клиент (чат, cliAgent, openai SDK) ──Bearer gw-…──▶ шлюз :7700 ──mTLS + UPSTREAM_API_KEY──▶ инференс
```

## Установка
```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt -c requirements.lock        # Windows: venv\Scripts\pip install -r requirements.txt
cp .env.example .env
venv/bin/uvicorn --factory gateway.app:create_app --host 0.0.0.0 --port 7700
```

## Настройка
1. Положить сертификаты в `certs/` (папка `chmod 700`, файлы `chmod 600`):
   `client.crt`, `client.key`, при необходимости `ca.pem` — CA сертификата **сервера** инференса.
2. Заполнить `.env` (образец — `.env.example`): `UPSTREAM_BASE_URL` (с `/v1`), пути к сертификатам,
   пароль ключа `CLIENT_KEY_PASSWORD`, `UPSTREAM_API_KEY` — если апстриму нужен ещё и ключ.
3. `systemctl --user restart llm-gateway` и проверить `curl localhost:7700/health` → `{"status":"ok"}`.

Пока сертификаты/URL не заданы, шлюз работает и отвечает `503 gateway_not_configured` с причиной.
Срок сертификата пишется в лог при старте (предупреждение за 14 дней до истечения).

## Токены
```bash
./gwtoken create ivan --days 90 --rpm 30          # печатает токен один раз
./gwtoken create bot --models my-model            # только указанные модели
./gwtoken agent ivan default                      # доступ к агенту на сервере (--off — забрать)
./gwtoken list                                    # статус, срок, расход за 30 дней
./gwtoken revoke ivan                             # по имени или префиксу gw-xxxxxxx
./gwtoken usage [ivan] --days 7                   # запросы/ошибки/токены по моделям
```
В базе (`data/gateway.db`) хранится только sha256 токена. Тексты запросов не логируются.

## Использование
- Чат: `http://<сервер>:7700/chat/` — ввести токен, выбрать модель.
- API: `base_url = http://<сервер>:7700/v1`, `api_key = gw-…` (также принимается заголовок `x-api-key`).
  Эндпоинты: `GET /v1/models`, `POST /v1/chat/completions` (в т.ч. stream), `/v1/completions`, `/v1/embeddings`.
- cliAgent: провайдер `"options": {"baseURL": "http://<сервер>:7700/v1", "apiKey": "gw-…"}`.

## Агент
Поверх шлюза есть агент: модель вызывает инструменты (файлы, shell, `web_fetch`/`http_request`/`web_search`,
MCP-серверы), права allow/ask/deny и хуки решают, что можно; сессии — в SQLite, настройки — в JSON.
Два интерфейса на одном ядре (`agent/`):

- **на сервере** — `/agent/*` и режим «Агент» в веб-чате; инструменты выполняются на машине шлюза.
  Доступ выдаётся токену явно: `./gwtoken create ivan --agent default` (профили — `config/agent.json`,
  образец `config/agent.json.example`);
- **в терминале** — `./gwagent` (модель через шлюз по `GW_TOKEN`, инструменты — локально):
  `GW_TOKEN=gw-… ./gwagent -p "задача"` или интерактивно `./gwagent`.

Документация — **[вики](docs/wiki/Home.md)**: настройки, инструменты, MCP, хуки, права, API, безопасность.

## Ограничения и защита
- `MAX_REQUEST_BODY_BYTES` (10 МБ), `--rpm` на токен, `MODEL_MAX_CONNECTIONS` — одновременных запросов на модель.
- `GATEWAY_MODE=PROD` — без `/docs`. CORS выключен (чат открывается с того же адреса).
- Шлюз отдаёт HTTP: снаружи локальной сети ставить за nginx с TLS.

## Разработка
```bash
venv/bin/pytest -q    # поднимает настоящий mTLS-апстрим с CERT_REQUIRED; всё офлайн
```
Устройство тестов — [docs/wiki/testing.md](docs/wiki/testing.md).
Логи: `journalctl --user -u llm-gateway -f`. После правок кода — `systemctl --user restart llm-gateway`
(страница чата — статика, перезапуск не нужен).


## Бенчмарки и провайдеры

Отдельные проверки модели: скорость, контекстное окно, «иголка в стоге сена», арифметика,
вызовы инструментов, JSON по схеме и цепочки действий. Результаты — JSON и Markdown;
подробности и ограничения — в [вики бенчмарков](docs/wiki/bench/README.md).

```bash
venv/bin/python -m bench --model MODEL --quick
venv/bin/python -m agent.providers add local --url http://127.0.0.1:8000/v1 --model MODEL
./gwagent --provider local
venv/bin/python -m bench --provider local --suites tools,structured,chain --quick
```

[Менеджер провайдеров](docs/wiki/providers.md) общий для CLI агента и бенчмарков:
именованные URL, модели и ключи из окружения, список моделей и сохранённый выбор подключения.


## Установка CLI и настройка подключения

Linux, Python 3.12+. Установка пакета: `python -m pip install . -c requirements.lock`.
Доступны gwagent, gwproviders, gwbench; из checkout сохраняются ./gwagent и python -m bench.

```bash
gwproviders add corp --url https://llm.example.org/v1 --model MODEL \
  --cert /path/client.crt --key /path/client.key --ca /path/ca.pem
gwproviders connect corp               # скрытый ввод ключа/пароля
gwproviders use corp
gwagent doctor
gwagent trust                          # если в проекте есть .gwagent/settings.json
gwagent
```

Подключения — ~/.gwagent/providers.json, секреты — отдельный auth.json (0600),
сертификаты остаются файлами. Добавление записи не меняет активный провайдер; нужен use.
В агенте доступны /connect, /compact, /export и /delete. Подробнее:
[провайдеры](docs/wiki/providers.md), [CLI](docs/wiki/cli.md), [результат аудита](docs/wiki/technical-debt.md).

Проверки разработчика: `pip install -r requirements-dev.txt -c requirements.lock`,
`python -m pytest -q`, `node --test tests/test_chat.mjs`.

## Терминальный интерфейс OpenTUI

При установленном [Bun](https://bun.sh) >= 1.3.14:

```bash
./gwagent tui install
./gwagent --tui
```

Потоковые ответы с Markdown, таблицами из JSON, выделением важных замечаний,
карточками инструментов и скрытым вводом ключей. После установки `gwagent` выбирает
TUI в интерактивном терминале; `--plain` сохраняет обычный CLI, `-p ... --json` — скриптовый режим.
[Управление и ограничения](docs/wiki/tui.md).

Единый каталог агента — `~/.gwagent`; другой путь: `GWAGENT_HOME=/srv/gwagent ./gwagent`. Расположение файлов: `./gwagent data`; перенос старых настроек и истории: `./gwagent data migrate` (после завершения других CLI, без перезаписи существующих файлов). [Подробнее](docs/wiki/data.md).
