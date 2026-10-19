# Агент на сервере: API `/agent/*`

Код: [`gateway/agent_api.py`](../../gateway/agent_api.py) (подключается в `create_app`, [`gateway/app.py`](../../gateway/app.py)).
Тесты: [`tests/test_agent_api.py`](../../tests/test_agent_api.py) — против настоящего mTLS-апстрима со сценарием ответов.

Серверный агент выполняет инструменты **на машине шлюза**, в рабочей папке сессии
`<workspace_root>/<id токена>/<id сессии>`. Модель вызывается напрямую через mTLS-клиент шлюза
(без круга через `/v1`); каждое обращение к модели пишется в `usage` с путём `agent`
(`./gwtoken usage` его показывает).

## Доступ

1. Профили — `config/agent.json` (образец [`config/agent.json.example`](../../config/agent.json.example),
   путь — `AGENT_CONFIG` в `.env`; нет файла — один профиль `default` с умолчаниями). Ключи — как в
   [settings.md](settings.md). Файл читается при старте: после правки — `systemctl --user restart llm-gateway`.
2. Токену назначается профиль: `./gwtoken create ivan --agent default` или
   `./gwtoken agent ivan readonly` (забрать — `./gwtoken agent ivan --off`).
3. Аутентификация — как у `/v1`: `Authorization: Bearer gw-…` или `x-api-key`; rpm токена считается
   по каждому HTTP-запросу. Allowlist моделей токена проверяется при создании сессии и каждом сообщении.

В хуках профиля доступна переменная `GATEWAY_ROOT` (папка проекта): `"$GATEWAY_ROOT/config/hooks/check_shell.py"`.
MCP-серверы профиля поднимаются один раз при первом обращении и общие для всех сессий профиля.

## Эндпоинты

| Метод и путь | Тело | Ответ |
|---|---|---|
| `POST /agent/sessions` | `{"model": "…", "title": "…"}` (`model` можно не указывать, если он есть в профиле) | сессия |
| `GET /agent/sessions` | — | `{"data": [сессия + "running"]}`, свежие сверху |
| `GET /agent/sessions/{id}` | — | сессия + `running`, `pending` (id вызовов, ждущих подтверждения), `messages`, `tool_runs` |
| `DELETE /agent/sessions/{id}` | — | `{"deleted": id}`; отменяет идущий запрос, удаляет историю **и рабочую папку** (только если она внутри `workspace_root`) |
| `POST /agent/sessions/{id}/messages` | `{"content": "…"}` | поток SSE событий агента |
| `POST /agent/sessions/{id}/approve` | `{"id": "<call id>", "decision": "allow" \| "deny" \| "always"}` | `{"id", "decision"}` |
| `POST /agent/sessions/{id}/cancel` | — | `{"cancelled": true}` или `false`, если ничего не шло |
| `GET /agent/tools` | — | инструменты профиля (`name`, `description`, `default_permission`), режим прав, статус MCP |

Сессия: `{"id", "title", "profile", "model", "workspace", "allow_rules", "created", "updated"}`.
Чужая сессия неотличима от несуществующей (404).

## Поток событий

`POST …/messages` отвечает `text/event-stream`. Каждое событие:

```
event: tool_result
data: {"type": "tool_result", "id": "call_1", "name": "write_file", "ok": true, "content": "Создан hello.txt (2 байт)", "decision": "allow", "ms": 3}
```

Первое — `{"type": "session", "id", "model"}`, дальше события цикла ([agent-loop.md](agent-loop.md)),
последнее — `done` (`stop`, `max_steps`, `error`, `blocked` или `cancelled`). Пока агент ждёт
подтверждения, раз в 15 с приходит комментарий `: ping` (чтобы nginx/прокси не закрыли поток).

**Подтверждение**: пришло `permission_request` с `id` → отправьте `POST …/approve` с этим `id`
отдельным запросом, поток продолжится. Не ответили за `permissions.timeout` — отказ.

**Отмена**: `POST …/cancel` или разрыв соединения с потоком. Недошедшие вызовы получают в истории
«Отменено пользователем», поток заканчивается `done(cancelled)`.

В сессии одновременно выполняется один запрос: второй `POST …/messages` — 409 `session_busy`.

## Пример (curl)

```bash
T=gw-…; H="Authorization: Bearer $T"; U=http://192.168.0.159:7700
SID=$(curl -s -H "$H" -H 'content-type: application/json' -d '{"model":"my-model"}' $U/agent/sessions | jq -r .id)
curl -N -H "$H" -H 'content-type: application/json' -d '{"content":"создай hello.txt со словом hi"}' \
     $U/agent/sessions/$SID/messages
# в другом окне, когда пришёл permission_request с id call_1:
curl -H "$H" -H 'content-type: application/json' -d '{"id":"call_1","decision":"allow"}' $U/agent/sessions/$SID/approve
```

## Ошибки

Формат как у `/v1` (`{"error": {"message", "type", "code"}}`):

| Код | `code` | Когда |
|---|---|---|
| 401 | `missing_api_key` / `invalid_api_key` | нет или плохой токен |
| 403 | `agent_not_allowed` | у токена нет профиля агента |
| 403 | `agent_profile_missing` | профиля токена нет в `config/agent.json` |
| 403 | `model_not_allowed` | модель вне allowlist токена |
| 400 | `model_required`, `empty_message`, `bad_decision` | неверный запрос |
| 404 | `session_not_found`, `approval_not_found` | нет такой сессии / ожидающего подтверждения |
| 409 | `session_busy` | в сессии уже идёт запрос |
| 503 | `agent_not_configured` | `config/agent.json` не прошёл проверку (причина в сообщении и в логе) |
| 503 | `gateway_not_configured` | апстрим/сертификаты не настроены |

## Ограничения

- Лимит `MODEL_MAX_CONNECTIONS` к агенту не применяется (он только для `/v1`).
- Кэш собранных агентов — до 200 сессий в памяти; хук `SessionStart` срабатывает заново, когда
  сессия вытеснена из кэша или сервер перезапущен.
- Идущие запросы живут в памяти процесса: перезапуск шлюза их обрывает (история в SQLite остаётся).
