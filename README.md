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
venv/bin/pip install -r requirements.txt        # Windows: venv\Scripts\pip install -r requirements.txt
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

## Ограничения и защита
- `MAX_REQUEST_BODY_BYTES` (10 МБ), `--rpm` на токен, `MODEL_MAX_CONNECTIONS` — одновременных запросов на модель.
- `GATEWAY_MODE=PROD` — без `/docs`. CORS выключен (чат открывается с того же адреса).
- Шлюз отдаёт HTTP: снаружи локальной сети ставить за nginx с TLS.

## Разработка
```bash
venv/bin/pytest -q    # поднимает настоящий mTLS-апстрим с CERT_REQUIRED
```
Логи: `journalctl --user -u llm-gateway -f`. После правок кода — `systemctl --user restart llm-gateway`
(страница чата — статика, перезапуск не нужен).
