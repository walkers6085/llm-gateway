# Хуки

Код: [`agent/hooks.py`](../../agent/hooks.py). Тесты: [`tests/test_hooks.py`](../../tests/test_hooks.py),
хуки внутри цикла — [`tests/test_loop.py`](../../tests/test_loop.py).

Хук — внешняя команда, которую агент запускает на событие. Через хуки делаются политики
(запретить опасную команду), аудит, автоформатирование после правки, подсказки модели.

## Настройка

```json
"hooks": {
  "PreToolUse":  [{"matcher": "shell", "command": "~/.gwagent/hooks/check_shell.py", "timeout": 10}],
  "PostToolUse": [{"matcher": "write_file|edit_file", "command": "ruff format \"$(jq -r .tool_input.path)\""}],
  "SessionStart": [{"command": "cat .gwagent/context.md | jq -Rs '{additional_context: .}'"}]
}
```

| Поле | Описание |
|---|---|
| `matcher` | регулярное выражение на **полное** имя инструмента (`re.fullmatch`); пусто или `*` — все. Для событий без инструмента игнорируется. |
| `command` | команда для `/bin/sh -c`, cwd — рабочая папка сессии |
| `timeout` | секунд, по умолчанию 30; по истечении процесс (вся группа) убивается |

Хуки одного события выполняются по порядку. `deny` останавливает цепочку.

## События

| Событие | Когда | Поля во входном JSON | Что можно вернуть |
|---|---|---|---|
| `SessionStart` | первый запрос в процессе (новая сессия или продолжение) | `model` | `additional_context` — добавляется в системный промпт |
| `UserPromptSubmit` | перед отправкой запроса пользователя | `prompt` | `deny` — запрос отклонён; `additional_context` — дописывается к запросу в `<context>` |
| `PreToolUse` | перед вызовом инструмента (аргументы уже проверены) | `tool_name`, `tool_input` | `allow` / `ask` / `deny`; `updated_input` — заменить аргументы |
| `PostToolUse` | после выполнения (не вызывается, если вызов отклонён) | `tool_name`, `tool_input`, `tool_output`, `is_error` | `deny` — модель вместо результата получит причину; `additional_context` — дописывается к результату |
| `Stop` | модель ответила без вызовов инструментов | `last_message` | `deny` + `reason` — не останавливаться, `reason` уходит модели репликой пользователя |

Во всех событиях есть `event`, `session_id`, `cwd`. В окружении команды — `GWAGENT_EVENT`,
`GWAGENT_SESSION`, `GWAGENT_CWD`.

## Ответ хука

| Результат | Что происходит |
|---|---|
| код 0, stdout пуст или не JSON | продолжить |
| код 0, stdout — JSON-объект | поля `decision` (`allow`/`ask`/`deny`), `reason`, `updated_input`, `additional_context` |
| код 2 | **блокировка** (`deny`), причина — stderr |
| другой код, таймаут, не запустился | событие `warning`, работа продолжается как без хука |

Если несколько хуков вернули решение — побеждает самое строгое (`deny` > `ask` > `allow`).
`updated_input` передаётся по цепочке: следующий хук видит уже изменённые аргументы; итог снова
проверяется схемой инструмента. Решение хука для прав — шаг 2 порядка из [permissions.md](permissions.md):
`deny`-правила из настроек хук отменить не может.

## Пример: запрет опасных команд

```python
#!/usr/bin/env python3
import json, re, sys
data = json.load(sys.stdin)
cmd = data["tool_input"].get("command", "")
if re.search(r"\b(sudo|mkfs|dd\s+if=)|rm\s+-rf\s+[/~]", cmd):
    print(f"Команда запрещена политикой: {cmd}", file=sys.stderr)
    sys.exit(2)
if cmd.startswith(("git status", "git diff", "ls")):
    print(json.dumps({"decision": "allow"}))
```

## Безопасность

Хуки выполняются с правами процесса агента. На сервере хуки задаются только в профилях
`config/agent.json` (администратором), пользователи API их не меняют.

## Отмена и пределы ресурсов

stdout/stderr вычитываются параллельно с ограничением сохранённого вывода до 64 KiB на поток.
Отмена, таймаут и завершение родителя очищают всю группу процессов и задачи чтения. Хук,
оставивший потомка с открытым stdout, ограничен общим таймаутом. Проектные хуки не запускаются
до доверия точному содержимому `.gwagent/settings.json` (gwagent trust).

## Корреляция и ранний выход

В payload цикла передаются `request_id`, `span_id`; окружение хуков содержит
`GWAGENT_REQUEST_ID`, `GWAGENT_SPAN_ID`. Они связывают действия хука с моделью/инструментом.
Если процесс закончил работу, не прочитав stdin, BrokenPipe/ConnectionReset при записи
не прерывает агента: stdout, stderr и код завершения всё равно обрабатываются.
Дочерний агент наследует конфигурацию хуков и окружение, но имеет собственную session_id.
