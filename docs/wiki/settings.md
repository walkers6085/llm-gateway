# Настройки (JSON)

Код: [`agent/settings.py`](../../agent/settings.py), валидатор — [`agent/schema.py`](../../agent/schema.py).
Тесты: [`tests/test_settings.py`](../../tests/test_settings.py).

Все настройки агента — один JSON-объект. Секреты (токен шлюза, ключи MCP-серверов) лучше
передавать через переменные окружения, а не писать в файл: для токена есть `gateway.token_env`,
в `mcp.servers.*.env`/`headers` значения вида `${VAR}` подставляются из окружения.

## Откуда берутся

**CLI (`gwagent`)** — слои, каждый следующий главнее:

1. встроенные умолчания (`DEFAULTS` в коде, они же — таблица ниже);
2. `~/.config/gwagent/settings.json` — пользователь;
3. `<рабочая папка>/.gwagent/settings.json` — проект;
4. файл из `--settings <путь>`;
5. флаги командной строки (`--model`, `--allow-all`, …).

**Сервер (шлюз)** — файл `config/agent.json` (путь меняется переменной `AGENT_CONFIG` в `.env`):

```json
{
  "profiles": {
    "default":  { "permissions": { "deny": ["shell"] } },
    "power":    { "max_steps": 50, "permissions": { "allow": ["shell(git *)"] } },
    "readonly": { "tools": { "enabled": ["read_file", "list_dir", "grep", "web_search", "web_fetch"] },
                  "permissions": { "mode": "read_only" } }
  }
}
```

Каждый профиль накладывается на умолчания. Токену назначается профиль: `./gwtoken create ivan --agent power`.
Если файла нет — есть один профиль `default` с умолчаниями. Образец — `config/agent.json.example`.

## Правила слияния

- словари сливаются по ключам рекурсивно: `{"shell": {"timeout": 10}}` меняет только `timeout`;
- списки и скаляры **заменяются целиком**: `tools.enabled: ["read_file"]` оставит один инструмент;
- `null` — обычное значение (например, `temperature: null` — не передавать температуру модели).

## Проверка

Каждый слой проверяется отдельно, ошибка называет файл и путь до ключа:

```
~/.config/gwagent/settings.json: shell.timeout: ожидается integer, получено str
.gwagent/settings.json: sheel: неизвестный ключ
```

Неизвестные ключи — ошибка (защита от опечаток). Исключения — словари со свободными ключами:
`mcp.servers`, `shell.env`, `mcp.servers.*.env`, `mcp.servers.*.headers`.

## Справка по ключам

| Ключ | Тип | По умолчанию | Описание |
|---|---|---|---|
| `model` | string | `""` | Модель. В CLI пусто — берётся первая из `/v1/models`. На сервере модель задаётся при создании сессии. |
| `system_prompt` | string | см. код | Системный промпт. Агент дописывает к нему рабочую папку, дату и подсказки хуков. |
| `temperature` | number \| null | `null` | Температура (0–2), `null` — не передавать. |
| `max_steps` | int ≥ 1 | `25` | Сколько раз за один запрос пользователя агент может обратиться к модели. |
| `max_tool_output_chars` | int ≥ 1 | `30000` | Результат инструмента длиннее — обрезается (с пометкой) перед отправкой модели. |
| `tools` | object | | Набор инструментов. |
| `tools.enabled` | string[] | все встроенные | Включённые встроенные инструменты. MCP-инструменты включаются через `mcp.servers`. |
| `permissions` | object | | Права, см. [permissions.md](permissions.md). |
| `permissions.mode` | `default` \| `allow_all` \| `read_only` | `default` | `default` — у каждого инструмента своё умолчание; `allow_all` — ничего не спрашивать (кроме `deny`); `read_only` — всё, что требует подтверждения, запрещено. |
| `permissions.allow` | string[] | `[]` | Правила «разрешить без вопроса», например `shell(git status*)`. |
| `permissions.ask` | string[] | `[]` | Правила «всегда спрашивать». |
| `permissions.deny` | string[] | `[]` | Правила «запретить». Сильнее всего остального. |
| `permissions.timeout` | int ≥ 1 | `600` | Сколько секунд ждать ответа на запрос подтверждения; потом — отказ. |
| `files` | object | | Файловые инструменты, см. [tools/files.md](tools/files.md). |
| `files.max_read_bytes` | int ≥ 1 | `2000000` | Файлы больше не читаются `read_file`/`grep`. |
| `shell` | object | | Инструмент `shell`, см. [tools/shell.md](tools/shell.md). |
| `shell.executable` | string | `/bin/bash` | Оболочка (`<executable> -c <команда>`). |
| `shell.timeout` | int ≥ 1 | `120` | Таймаут команды по умолчанию, с. |
| `shell.max_timeout` | int ≥ 1 | `600` | Больше этого модель не может запросить через аргумент `timeout`. |
| `shell.env_passthrough` | string[] | `PATH, HOME, LANG, LC_ALL, TERM, TZ, USER` | Какие переменные окружения процесса передаются команде. Остальные (токены, ключи) — нет. |
| `shell.env` | object | `{}` | Дополнительные переменные для команд. |
| `network` | object | | Сетевые инструменты, см. [tools/web.md](tools/web.md). |
| `network.timeout` | int ≥ 1 | `30` | Таймаут запроса, с. |
| `network.max_bytes` | int ≥ 1 | `2000000` | Сколько байт ответа читать максимум. |
| `network.max_redirects` | int ≥ 0 | `5` | Сколько редиректов проходить (каждый проверяется заново). |
| `network.proxy` | string | `""` | Явный прокси (`http://host:port`). Системные `HTTP(S)_PROXY` не используются. |
| `network.user_agent` | string | `Mozilla/5.0 … gwagent/0.1` | Заголовок `User-Agent`. |
| `network.allow_private` | bool | `false` | Разрешить адреса локальной сети/loopback (по умолчанию запрещены — защита от SSRF). |
| `network.allow_hosts` | string[] | `[]` | Хосты/IP, которым можно быть приватными (`127.0.0.1`, `intranet.local`). |
| `network.allow_domains` | string[] | `[]` | Если не пусто — ходить можно только на эти домены (и их поддомены). |
| `network.deny_domains` | string[] | `[]` | Запрещённые домены (и поддомены). |
| `search` | object | | Инструмент `web_search`. |
| `search.provider` | `duckduckgo` \| `searxng` | `duckduckgo` | Поисковик. |
| `search.duckduckgo_url` | string | `https://html.duckduckgo.com/html/` | Адрес HTML-версии DuckDuckGo. |
| `search.searxng_url` | string | `""` | Адрес своего SearXNG (`https://searx.example/`), нужен для `provider: searxng`. |
| `search.max_results` | int ≥ 1 | `8` | Сколько результатов отдавать модели. |
| `mcp` | object | | MCP-серверы, см. [mcp.md](mcp.md). |
| `mcp.timeout` | int ≥ 1 | `60` | Таймаут запроса к MCP-серверу по умолчанию, с. |
| `mcp.servers` | object | `{}` | `{"<имя>": {"type": "stdio", "command": …} \| {"type": "http", "url": …}}`. |
| `hooks` | object | `{}` | Хуки по событиям, см. [hooks.md](hooks.md). |
| `gateway` | object | | Куда CLI ходит за моделью (на сервере не используется). |
| `gateway.url` | string | `http://127.0.0.1:7700/v1` | Базовый URL шлюза с `/v1`. |
| `gateway.token_env` | string | `GW_TOKEN` | Из какой переменной окружения брать токен `gw-…`. |
| `gateway.token` | string | — | Токен прямо в настройках (не рекомендуется). |
| `sessions` | object | | Хранилище сессий CLI. |
| `sessions.db` | string | `~/.local/share/gwagent/sessions.db` | Файл SQLite. На сервере сессии лежат в `DB_PATH` шлюза. |
| `workspace_root` | string | `data/workspaces` | Только сервер: корень рабочих папок; у сессии своя `<корень>/<id токена>/<id сессии>`. Относительный путь — от папки проекта. |

## Пример `~/.config/gwagent/settings.json`

```json
{
  "gateway": { "url": "http://192.168.0.159:7700/v1" },
  "model": "my-model",
  "permissions": { "allow": ["shell(git status*)", "shell(ls*)", "web_fetch"] },
  "mcp": { "servers": { "fs": { "type": "stdio", "command": "npx",
                                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"] } } }
}
```
