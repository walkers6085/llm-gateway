# CLI: gwagent

Код: [`agent/cli.py`](../../agent/cli.py), запуск — [`gwagent`](../../gwagent) (как `gwtoken`, сам
перезапускается из `venv/`). Тесты: [`tests/test_cli.py`](../../tests/test_cli.py) — против настоящего шлюза
в потоке и mTLS-апстрима.

Модель — через шлюз по токену `gw-…` (лимиты, allowlist и учёт токена действуют), инструменты,
MCP-серверы и хуки — **на машине, где запущен gwagent**, в текущей папке.

## Быстрый старт

```bash
export GW_TOKEN=gw-…                                   # токен от ./gwtoken create
mkdir -p ~/.config/gwagent
echo '{"gateway": {"url": "http://192.168.0.159:7700/v1"}}' > ~/.config/gwagent/settings.json
cd ~/projects/demo && /home/deb/llm-gateway/gwagent    # или ln -s …/gwagent ~/.local/bin/
```

Для CLI профиль агента у токена **не нужен**: агент работает локально и ходит только в `/v1`.

## Параметры

| Флаг | Описание |
|---|---|
| `-p, --prompt "текст"` | один запрос и выход; код выхода 0, если агент закончил (`done: stop`), иначе 1; 2 — ошибка настройки |
| `--cwd DIR` | рабочая папка (по умолчанию текущая); из неё же читается `.gwagent/settings.json` |
| `--model M` | модель (иначе `model` из настроек, иначе первая из `/v1/models`); сохраняется в сессии |
| `--settings FILE` | дополнительный слой настроек |
| `--resume ID` | продолжить сессию (достаточно однозначного начала id) |
| `-c, --continue` | продолжить последнюю сессию с этой рабочей папкой |
| `--allow-all` | `permissions.mode = allow_all` |
| `--read-only` | `permissions.mode = read_only` |
| `--json` | события построчно в JSON (для скриптов; формат — [agent-loop.md](agent-loop.md)) |

Токен: `gateway.token` в настройках или переменная из `gateway.token_env` (по умолчанию `GW_TOKEN`).

## Интерактивный режим

```
gwagent · my-model · /home/me/demo · сессия 3fa2c1d09e7b4410 · /help
› найди TODO в проекте и составь список
⚙ grep {"pattern": "TODO"}
  ✓ src/app.py:12:# TODO: кэш (4 мс)
Нашёл 3 места: …
```

| Команда | Что делает |
|---|---|
| `/new` | новая сессия (модель и папка те же) |
| `/sessions` | 20 последних сессий, `*` — текущая |
| `/resume <id>` | переключиться на сессию |
| `/model [имя]` | показать / сменить модель сессии |
| `/tools` | инструменты с правами по умолчанию |
| `/mcp` | MCP-серверы: инструменты или ошибка запуска |
| `/help`, `/exit` | справка, выход (также Ctrl+D) |

**Ctrl+C** во время работы отменяет запрос (история остаётся корректной), в приглашении — просто
новая строка. Редактирование строки и история ввода — через `readline`.

## Подтверждения

```
? shell: rm -rf build
  (требует подтверждения; «всегда» добавит правило shell(rm -rf build))
  Разрешить? [y]es / [n]o / [a]lways:
```

`y` — разрешить, `n` или Enter — отказать, `a` — разрешить и запомнить правило в сессии.
Ctrl+C на вопросе — отказ и отмена запроса. В режиме `-p` без терминала (cron, пайплайн) спросить
некого — всё, что требует подтверждения, отклоняется; используйте `--allow-all` или `allow`-правила.

## Где что лежит

- настройки: `~/.config/gwagent/settings.json`, `<папка>/.gwagent/settings.json` ([settings.md](settings.md));
- сессии: `~/.local/share/gwagent/sessions.db` (`sessions.db`), владелец `local` ([sessions.md](sessions.md)).

## Примеры

```bash
gwagent -p "обнови README по коду" --allow-all
gwagent -p "сводка по логам" --read-only --json | jq -r 'select(.type=="text").text'
gwagent -c                                       # продолжить вчерашнюю сессию в этой папке
```
