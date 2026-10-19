# Сессии (SQLite)

Код: [`agent/store.py`](../../agent/store.py). Тесты: [`tests/test_store.py`](../../tests/test_store.py).

Сессия — это диалог с агентом: история сообщений, рабочая папка, модель и разрешения «всегда».
Хранится в SQLite (stdlib `sqlite3`, режим WAL — читатели не блокируют писателя).

| Где | Файл | Владелец сессии (`owner`) |
|---|---|---|
| Сервер | `DB_PATH` шлюза (`data/gateway.db`) — та же база, что и токены | id токена (строкой) |
| CLI | `sessions.db` из настроек (`~/.local/share/gwagent/sessions.db`) | `local` |

## Схема

```sql
agent_sessions (
  id TEXT PRIMARY KEY,          -- 16 hex-символов, secrets.token_hex(8)
  owner TEXT,                   -- кому принадлежит; чужую сессию API не показывает
  title TEXT,                   -- по умолчанию — начало первого сообщения
  profile TEXT,                 -- профиль настроек (сервер)
  model TEXT,
  workspace TEXT,               -- абсолютный путь рабочей папки
  allow_rules TEXT,             -- JSON-массив правил, добавленных ответом «всегда»
  created REAL, updated REAL    -- unix time
)
agent_messages (
  session_id → agent_sessions.id ON DELETE CASCADE,
  seq INTEGER,                  -- 1, 2, 3… внутри сессии
  role TEXT,                    -- user | assistant | tool
  data TEXT,                    -- сообщение целиком, JSON в формате OpenAI
  ts REAL
)
agent_tool_runs (               -- журнал аудита вызовов инструментов
  session_id, call_id, tool,
  args TEXT,                    -- JSON аргументов (после правки хуком)
  decision TEXT,                -- allow | deny | ask:allow | ask:deny | error (вызов не дошёл до прав)
  ok INTEGER, ms INTEGER, ts REAL
)
agent_meta (key, value)         -- schema_version
```

Системный промпт в истории **не хранится** — он собирается заново при каждом запуске из
текущих настроек (иначе смена промпта не действовала бы на старые сессии).

### Что лежит в `data`

```json
{"role": "user", "content": "создай hello.txt"}
{"role": "assistant", "content": "Создаю файл.", "tool_calls": [
  {"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": "{\"path\":\"hello.txt\",\"content\":\"hi\"}"}}]}
{"role": "tool", "tool_call_id": "call_1", "content": "Записано 2 байт в hello.txt"}
{"role": "assistant", "content": "Готово."}
```

Инвариант: за каждым `assistant` с `tool_calls` идут `tool`-сообщения на **все** его `id`.
Агент поддерживает его и при ошибках/отмене (недошедшие вызовы получают результат
«Отменено»), иначе апстрим отклонит историю при продолжении сессии.

## Миграции

Версия схемы — в таблице `agent_meta` (`schema_version`), **не** в `PRAGMA user_version`:
на сервере база общая с токенами шлюза. При открытии `SessionStore` применяет недостающие
миграции из словаря `MIGRATIONS` по порядку, каждую в своей транзакции. Новая миграция:
добавить `MIGRATIONS[N]`, поднять `SCHEMA_VERSION`, дописать тест и этот раздел.

## API класса

| Метод | Что делает |
|---|---|
| `create(owner, title=, profile=, model=, workspace=)` | новая сессия |
| `get(id, owner=None)` | сессия; с `owner` — только своя |
| `find(prefix, owner=None)` | по однозначному префиксу id (`/resume 3fa2`) |
| `list(owner, limit=50)` | свежие сверху |
| `update(id, title=, model=, workspace=, allow_rules=)` | правка полей, обновляет `updated` |
| `delete(id)` | вместе с сообщениями и журналом |
| `add_message(id, message)` / `messages(id)` | история |
| `record_tool(...)` / `tool_runs(id)` | журнал вызовов |

## Где смотреть руками

```bash
sqlite3 data/gateway.db "SELECT id, owner, title, datetime(updated,'unixepoch') FROM agent_sessions"
sqlite3 data/gateway.db "SELECT tool, decision, ok, ms FROM agent_tool_runs WHERE session_id='…'"
```
