# Тесты

```bash
venv/bin/pytest -q                              # всё (≈20 с)
venv/bin/pytest -q tests/test_loop.py           # один модуль
venv/bin/pytest -q tests/test_cli.py::test_repl_approvals_commands_and_resume
```

Всё офлайн: интернет не нужен, внешние сервисы заменены локальными. На сервере мало RAM —
тесты и тяжёлые сборки параллельно не запускать.

## Устройство

| Файл | Что внутри |
|---|---|
| [`tests/fakes.py`](../../tests/fakes.py) | PKI (`make_pki`), фейковый апстрим (`upstream`, `SEEN`, `SCRIPT`), `ServerThread`, `scripted_transport`, `tool_call`, `auth` |
| [`tests/conftest.py`](../../tests/conftest.py) | запуск `async def test_*` (без pytest-asyncio), фикстуры `pki`, `upstream_url`, `gateway_settings`, `make` |
| [`tests/fixtures/`](../../tests/fixtures/) | игрушечный MCP-сервер (stdio), HTML выдачи DuckDuckGo |

- **Апстрим** — настоящий uvicorn с TLS и `CERT_REQUIRED`: шлюз обязан предъявить клиентский
  сертификат. Без сценария отвечает «привет»; со сценарием `SCRIPT` — по очереди отдаёт заданные
  ответы ассистента потоком SSE, разбивая аргументы `tool_calls` на куски (как настоящие модели).
- **Цикл агента** в `test_loop.py` идёт без сети: `LLMClient` поверх `httpx.MockTransport`
  (`scripted_transport`) и игрушечные инструменты.
- **Сервер** (`test_agent_api.py`) — `TestClient` шлюза + mTLS-апстрим; подтверждения проверяются
  параллельным потоком, который читает SSE, пока основной шлёт `approve`.
- **CLI** (`test_cli.py`) — шлюз в `ServerThread`, `amain()` с подменёнными вводом/выводом.
- **Сеть** (`test_tools_web.py`) — локальный FastAPI-сайт; проверки SSRF — на адресах, которые не
  требуют сети (`10.0.0.1`, `169.254.169.254`, IPv4-mapped).
- **MCP** (`test_mcp.py`) — `fixtures/mcp_stdio_server.py` (пагинация, уведомления, ping от сервера,
  падение, таймаут) и FastAPI-сервер Streamable HTTP (сессия, SSE-ответ, DELETE).
- **Вики** (`test_wiki.py`) — см. [Home.md](Home.md#правило-для-вики).

## Карта: модуль → тесты → страница

| Модуль | Тесты | Вики |
|---|---|---|
| `agent/settings.py`, `schema.py` | `test_settings.py` | [settings.md](settings.md) |
| `agent/store.py` | `test_store.py` | [sessions.md](sessions.md) |
| `agent/llm.py` | `test_llm.py` | [llm-client.md](llm-client.md) |
| `agent/loop.py` | `test_loop.py` | [agent-loop.md](agent-loop.md) |
| `agent/permissions.py` | `test_permissions.py`, `test_loop.py` | [permissions.md](permissions.md) |
| `agent/hooks.py` | `test_hooks.py`, `test_loop.py` | [hooks.md](hooks.md) |
| `agent/tools/files.py` | `test_tools_files.py` | [tools/files.md](tools/files.md) |
| `agent/tools/shell.py` | `test_tools_shell.py` | [tools/shell.md](tools/shell.md) |
| `agent/tools/web.py` | `test_tools_web.py` | [tools/web.md](tools/web.md) |
| `agent/mcp.py` | `test_mcp.py`, `test_cli.py` | [mcp.md](mcp.md) |
| `gateway/agent_api.py` | `test_agent_api.py` | [server-api.md](server-api.md) |
| `agent/cli.py` | `test_cli.py` | [cli.md](cli.md) |
| `chat/index.html` | `test_agent_api.py` (страница), ручной прогон | [web-chat.md](web-chat.md) |
| `gateway/app.py` (прокси) | `test_gateway.py` | [README](../../README.md) |

## Сценарий для нового теста агента

```python
from tests.fakes import SCRIPT, tool_call

SCRIPT.extend([
    {"content": "Читаю", "tool_calls": [tool_call("c1", "read_file", path="a.txt")]},
    {"content": "Готово"},                      # финальный ответ без инструментов
    # {"status": 503, "error": "перегружен"},  # ошибка апстрима
])
```

Фикстура `make` очищает `SEEN` и `SCRIPT` до и после теста; в `SEEN` видно, что ушло апстриму
(тело запроса, заголовок авторизации).

## Бенчмарки модели

Отдельные реальные замеры: [bench/README.md](bench/README.md). Модули `bench/client.py`,
`bench/runner.py`, `bench/textgen.py`, `bench/suites/` и CLI проверяет `tests/test_bench.py`
офлайн; pytest не обращается к реальной модели. `tests/test_bench_tasks.py` проверяет
оценку инструментов, структурированного вывода и цепочек, включая отрицательные случаи.

## Провайдеры

`agent/providers.py` → `tests/test_providers.py` → [providers.md](providers.md): сохранение,
валидация, отсутствие секретов в файле и выводе, запрос каталога моделей через MockTransport.
`tests/test_cli.py` проверяет переключение провайдера, новую сессию и возобновление с
сохранённым подключением через настоящий локальный шлюз и mTLS-апстрим.

## Регрессии аудита и установка

`tests/test_hardening.py` проверяет доверие проекту, редиректы, SSE, отмену, восстановление пар,
бюджет/компактацию, права и маскирование секретов, mTLS провайдера, конфликты файлов, таймаут
regex, потоки SQLite, отчёты, возможности моделей и doctor. `tests/test_provider_sessions.py`
проверяет папку и назначение подключения. `tests/test_server_hardening.py` — отмену семафора,
ограничение chunked-тела, health/live и кэш прав. `tests/test_new_benchmarks.py` — новые методики.

`agent/storage.py`, `credentials.py`, `trust.py`, `doctor.py`, `streaming.py` проверяются этими
регрессиями; соответствующие страницы — [providers.md](providers.md), [settings.md](settings.md),
[sessions.md](sessions.md), [cli.md](cli.md), [llm-client.md](llm-client.md).

`node --test tests/test_chat.mjs` — сценарии DOM/fetch для экранирования, opt-in хранения токена,
успешного/оборванного SSE и подтверждения инструментов, без браузерных зависимостей.
GitHub Actions проверяет Python 3.12/3.13, Node 22, вики и сборку wheel. Это конфигурация CI,
не утверждение о прохождении удалённого workflow до публикации изменений.
Для воспроизводимого окружения: `pip install -r requirements-dev.txt -c requirements.lock`.
requirements.txt содержит runtime, pytest отделён в requirements-dev.txt. Контрольный lock
содержит точные версии проверенного Linux/Python 3.13 окружения. Проверка упаковки:
`python -m pip wheel --no-deps --wheel-dir /tmp/gateway-wheel .`.

Сборочный backend закреплён в pyproject.toml на setuptools 84.0.0; wheel проверяется установкой в отдельный каталог и запуском команд вне checkout.

## OpenTUI

`agent/tui.py`, `agent/tui_backend.py` → `tests/test_tui.py` → [tui.md](tui.md).
`tui/src/format.ts`, `view.ts`, `main.ts` → `bun run --cwd tui typecheck`,
`bun run --cwd tui test` (разбор, реальные кадры, клавиши, секреты) → [tui.md](tui.md).
CI устанавливает Bun 1.3.14, зависимости по bun.lock без install scripts, запускает эти проверки.
Python wheel включает исходники и lockfile UI; Bun остаётся необязательным для обычного CLI.

Регрессии запуска без модели: `test_plain_offline_commands_provider_add_set_resume_and_recovery`,
`test_offline_start_commands_and_provider_setup_then_chat` и нативный тест UI «без модели можно
писать». Проверяют отсутствие HTTP-запросов при запуске/локальных командах, отсутствие ключа,
восстановление после ошибок, новый `/connect` и успешный ответ без перезапуска.

Пресеты GLM/Mistral: `tests/test_providers.py` проверяет изоляцию DEFAULT_CONFIG между
экземплярами, обязательность соответствующего env-ключа и stored-auth без env; ключи не
попадают в providers.json, вывод списка или repr подключения.

Меню `/`: нативные тесты в `tui/src/view.test.ts` проверяют открытие при вводе/вставке,
фильтрацию, ↑/↓, Enter, Tab, Esc, обязательные аргументы, пустой результат, узкий экран,
обычный текст и приоритет диалогов. `tests/test_tui.py` проверяет реальный каталог включённых
builtin/MCP без модели, отсутствие выключенных инструментов и `/tools NAME` без выполнения.
`tests/test_cli.py` покрывает такую справку в обычном CLI.

Выбор модели: `tui/src/view.test.ts` проверяет текущую модель, реальную отправку `/model NAME`,
обновление заголовка, поиск/вставку, 50 моделей на узком экране, пустой результат, Esc/Ctrl+C,
пустой каталог и закрытие при смене сессии. `tests/test_tui.py` проверяет метаданные выбора,
сохранение модели в SQLite без новой сессии и фактическое поле model следующего запроса.

Общий каталог: `test_models_from_all_providers_switch_together_and_preserve_old_history`
проверяет два живых источника с одинаковыми ID, неработающий ключ с fallback, отключённый
источник, переключение пары, сохранение прежней истории, отсутствие её утечки в другой
провайдер, устаревший/подменённый выбор и меню `/provider`. Нативные тесты проверяют
отдельные строки одинаковых моделей, поиск провайдера и сообщения select обоих видов.

Общие окна: `tui/src/choice.test.ts` проверяет настоящие нативные строки, цвет и фон тега free,
разделы, мышь и пропуск отключённых вариантов. `view.test.ts` проверяет F2/F3/F4, Ctrl+P,
черновики, системные роли, поиск/выбор сессий, инструменты и авторизацию, Esc для секретов
и разрешений. `tests/test_tui.py::test_sessions_picker_resume_workspace_guard_and_confirmed_delete`
проверяет восстановление истории, другую папку/владельца и оба ответа подтверждения удаления.
Новые runtime-файлы theme/menus/choice включены в wheel; tsc проверяет все модули и тесты.

- `agent/tracing.py`, `agent/tools/subagent.py`, reasoning: `tests/test_tracing_reasoning.py`; страницы [цикл](agent-loop.md), [сессии](sessions.md), [сабагент](tools/subagent.md).
- `tui/src/view.test.ts`: скрытый reasoning, восстановление истории, компактные ошибки, индикатор работы.

Сценарный mTLS-апстрим `tests/fakes.py` записывает только три заголовка корреляции в SEEN.trace; тесты шлюза проверяют передачу ID и общие workspace сабагентов.

- `agent/checkpoints.py`, миграция 4: `tests/test_revert.py` проверяет дедупликацию объектов, сохранение ветки, откат shell, конфликты и отсутствие потери ручных файлов; [сессии](sessions.md).
- `tests/test_tui.py` проверяет `/revert` через отдельный процесс и mTLS: оба режима, отмена, отсутствие лишних API-вызовов; native `view.test.ts` проверяет выбор, возврат черновика, метрики и заголовок.

- `tui/src/terminal-input.test.ts`: полный и разорванный OSC RGB после восстановления истории,
  все границы пакетов, задержки SSH, bracketed paste, Unicode, Escape, переполнение и таймаут.
- `tui/src/view.test.ts`: копирование Unicode из редактора, выделение мышью в ответе, Ctrl+Y,
  сохранение отмены Ctrl+C и запрет копирования секретного ввода. Clipboard подменяется callback,
  тесты не меняют системный буфер пользователя.
- Ручная офлайн-проверка PTY: загрузка старой сессии, инъекция фрагментов RGB, проверка точного
  payload OSC 52 для черновика и ответа, Ctrl+D и восстановление терминала.

- `tui/src/approval.test.ts`: точная многострочная команда, приглушённое оформление,
  отказ по Enter по умолчанию, стрелки/мышь, правило сессии, правая кнопка, PageDown и низкий экран.
  Также проверяются Unicode, вертикальные стрелки, сохранение черновика во время ответа и разрешения.
- `terminal-input.test.ts`: CSI/SS3 стрелки с задержками SSH перемещают курсор и не вставляют хвосты.
- `approval.ts` включён в wheel через pyproject.toml; его страница — [TUI](tui.md).

`tests/test_compaction.py` проверяет `/compact` после одного хода и повторное открытие БД,
автосжатие крупных результатов, сохранение цели и последнего запроса, продолжение после
переполнения у провайдера без повторения инструмента, предел повторов и отдельный бюджет хода.
Тесты используют локальный scripted_transport, без внешних моделей и пользовательских ключей.

`tests/test_planning.py`: регистрация и ограничения goal/todo, сохранение состояния,
сжатие и откат, запрет инструмента, соответствие оценки отправленному запросу и отсутствие
побочных эффектов у просмотра контекста. `tests/test_tui.py` проверяет dashboard без вызова
модели и обновление после инструментов через настоящий JSONL-процесс. `tests/test_mcp.py`
проверяет обнаружение падения stdio и восстановление статуса после перезапуска.
`tui/src/sidebar.test.ts` проверяет native-кадры процентов, состояний задач/MCP, сохранение
панели при разборе ответа, переход в `/mcp` с черновиком, узкий экран и смену сессии.

`tests/test_agent_modes.py`: безопасный реестр plan, запрет записи при allow_all/разрешающем
хуке, ожидание question и продолжение той же цепочки, пропуск/отсутствие получателя,
сохранение режима и общей истории. `tests/test_tui.py` проверяет переключение без модели,
очередь на время выполняющегося build, запрет записи в следующем plan, таймаут вопроса.
`tests/test_cli.py` проверяет /mode и выбор ответа в обычном CLI. Native-тесты
`tui/src/agent-modes.test.ts` покрывают Tab, черновик, модальные окна, выбор/свободный ответ,
пропуск и закрытие вопроса. Миграции проверяются тестами хранилища и трассировки.

`tests/test_steering.py` проверяет уточнение во время потока, общий request_id,
завершение текущего инструмента/пропуск остатка пачки и валидную историю, восстановление
очереди после отмены/открытия БД, хуки, границы очереди, файловый откат уточнения и очистку
при /revert. `tests/test_tui.py` проверяет JSONL-подмешивание до done, поздний отказ,
отмену и повторный процесс. Native `tui/src/steering.test.ts` проверяет Enter, карточки,
счётчик, отсутствие дублей и возврат текста при гонке с завершением. Тест разрешений
обновлён: Enter во время генерации теперь отправляет уточнение, сохраняя модальный черновик.

TUI проверяется в CI на Bun 1.3.8 и 1.3.14: установка из lockfile, проверка типов и все тесты с настоящим нативным рендерером. Минимальная проверенная версия проекта — 1.3.8, несмотря на более высокий engines.bun у @opentui/core.
