# Права на инструменты

Код: [`agent/permissions.py`](../../agent/permissions.py). Тесты: [`tests/test_permissions.py`](../../tests/test_permissions.py),
подтверждения в цикле — [`tests/test_loop.py`](../../tests/test_loop.py).

Перед каждым вызовом решается: **allow** (выполнить), **ask** (спросить пользователя) или **deny**.

## Правила

Строка `имя` или `имя(шаблон)`:

| Правило | Что значит |
|---|---|
| `shell` | любой вызов `shell` |
| `shell(git *)` | команда, подходящая под шаблон (`fnmatch`: `*`, `?`, `[…]`) |
| `write_file(docs/*)` | путь (нормализованный, относительно рабочей папки) |
| `web_fetch(domain:example.com)` | хост URL — `example.com` или его поддомен |
| `http_request(https://api.github.com/*)` | URL целиком по шаблону |
| `mcp__github__*` | все инструменты MCP-сервера `github` (шаблон по имени) |

«Предмет», с которым сравнивается шаблон, задаёт инструмент (`Tool.subject`):
`shell` — команда, файловые — путь, сетевые — URL, `web_search` — запрос. У MCP-инструментов
предмета нет: правило с шаблоном к ним не применяется, только по имени.

Осторожно с `shell(git *)`: шаблон сравнивается со всей строкой, `git status; rm -rf ~` под него
тоже подходит. Для shell надёжнее `deny`-правила и хук `PreToolUse` с разбором команды.

## Порядок решения

1. `permissions.deny` — первое совпавшее правило → **deny** (сильнее всего, в т.ч. хуков и режимов).
2. Решение хука `PreToolUse`, если он его вернул: `deny` / `allow` / `ask`.
3. `permissions.allow` + правила «всегда» этой сессии → **allow**.
4. `permissions.ask` → **ask**.
5. Режим `allow_all` → **allow**.
6. Умолчание инструмента: `read_file`, `list_dir`, `glob`, `grep`, `web_search` — **allow**; остальные
   (`write_file`, `edit_file`, `shell`, `web_fetch`, `http_request`, все MCP) — **ask**.

Режим `read_only` превращает любое итоговое **ask** в **deny** (без вопросов, только безопасное).

## Режимы (`permissions.mode`)

| Режим | Для чего |
|---|---|
| `default` | интерактивно: CLI, веб-чат |
| `allow_all` | доверенная автоматика (`gwagent -p … --allow-all`) — спрашивает только по явным `ask`-правилам и хукам |
| `read_only` | ничего не меняющий агент: профиль сервера для просмотра/поиска |

## Подтверждение

На **ask** агент шлёт событие `permission_request` и ждёт ответа `allow` / `deny` / `always`
(`permissions.timeout`, по умолчанию 600 с; молчание = отказ):

- CLI: `[y]es / [n]o / [a]lways`;
- веб-чат: кнопки «Разрешить / Запретить / Всегда»;
- API: `POST /agent/sessions/{id}/approve` (см. [server-api.md](server-api.md)).

`always` сохраняет правило `Tool.always_rule(args)` в сессии: для shell — точная команда
(`shell(make test)`), для файлов — путь, для сети — URL, для MCP — имя инструмента.

## Пример

```json
"permissions": {
  "allow": ["shell(git status)", "shell(git diff*)", "web_fetch(domain:docs.python.org)"],
  "ask":   ["read_file(.env*)"],
  "deny":  ["shell(*sudo*)", "shell(*rm -rf*)", "write_file(.git/*)"]
}
```
