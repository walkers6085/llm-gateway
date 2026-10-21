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

TUI проверяется в CI на Bun 1.3.8 и 1.3.14: установка из lockfile, проверка типов и все тесты с настоящим нативным рендерером. Минимальная проверенная версия проекта — 1.3.8, несмотря на более высокий engines.bun у @opentui/core; сам launcher версию Bun не проверяет.

Мастер провайдера: tests/test_providers.py проверяет отмену без частичных записей, сохранение прежнего ключа, смену URL, восстановление при ошибке записи и настоящее соединение mTLS. tests/test_tui.py проверяет диалоги добавления/изменения, отключение, включение, удаление и отмену, tui/src/view.test.ts — запуск действий из меню.

`tests/test_data.py` проверяет общую смену путей через GWAGENT_HOME, MCP, изоляцию служебных файлов, перенос SQLite с активным WAL, отмену при конфликтах и вывод путей без секретов. Autouse-фикстура задаёт временный GWAGENT_HOME, JSONL-процессы получают тот же каталог.


`tests/test_opencode.py` проверяет JSONC, импорт без записи, перенос/экспорт секретов,
ограничения формата, точный HTTP-запрос и сохранение mTLS/ключей. CLI-тест проверяет
применение профилей и их сброс между провайдерами, JSONL-тест — импорт, настройку модели
и экспорт через диалоги. Карта: agent/opencode.py → tests/test_opencode.py → opencode.md;
agent/providers.py → те же тесты и test_providers.py → providers.md.

agent/retry.py и agent/context.py → tests/test_retry_cleanup.py → request-processing.md. Проверяются границы backoff, Retry-After, восстановление после 429, отсутствие повторного выполнения инструментов, отмена, новые уточнения, отсутствие повторов после частичного ответа и постоянных ошибок, целостность истории, точность JSON/Unicode и MCP structuredContent. TUI-тест проверяет ожидание 429 и Esc.

Сводка через модель: tests/test_model_compaction.py → agent/compaction.py, store.py, loop.py → [обработка запросов](request-processing.md), [сессии](sessions.md). Проверяются части истории, лимиты, резервный вариант, отмена, трассировка и отказ от устаревшего результата. Строгие права и глобальный стандарт проверяются tests/test_permissions.py; сохранение и блокировка записи через меню — tests/test_tui.py и tui/src/view.test.ts.

Окна моделей: tests/test_model_limits.py → agent/model_limits.py, llm.py, cli.py, loop.py → [обработка запросов](request-processing.md), [настройки](settings.md). Проверяются форматы API, aliases, ручные ограничения, разные провайдеры с одинаковым ID, выбор до первого чата, необязательный каталог, общий бюджет и оценка токенов. Панель окна и источника — tui/src/sidebar.test.ts.

Команды провайдеров: tests/test_provider_commands.py → agent/provider_commands.py, cli.py,
providers.py → [провайдеры](providers.md), [CLI](cli.md). Проверяются кавычки/табуляция,
отсутствующие аргументы, сохранение выбора, проверка только GET models, reload с изоляцией
истории нового адреса, отмена после последнего поля до записи, команды оболочки без Bun/LLM.
Реальные диалоги моста и меню без имени — tests/test_tui.py; сохранённые способы авторизации,
недоступные пункты и черновики — tui/src/view.test.ts. Basic/mTLS с дополнительным Bearer
при редактировании — tests/test_providers.py.

Регрессии ручного `/compact`: tests/test_model_compaction.py проверяет включение последнего завершённого хода, вызов модели при локальном режиме автосжатия, восстановление полной истории после прежних выдержек, повтор 429 и отсутствие изменения checkpoint при ошибке/таймауте/отсутствии подключения. tests/test_cli.py::test_compact_failure_reports_error_without_changing_context проверяет сообщение об ошибке и продолжение чата через настоящий локальный шлюз.

`tests/test_tui.py::test_compact_notices_survive_snapshot_and_failure_can_be_retried` проверяет JSONL-мост: начало суммаризации, сохранение контекста при 401, повторную успешную команду и отсутствие history в snapshot, чтобы UI не стирал результат.

Регрессии видимости повторов суммаризации: tests/test_model_compaction.py проверяет уведомление до повторной попытки 429/502, а также выход из автосжатия во время backoff без изменения checkpoint и повторного запроса. JSONL-тест /compact в tests/test_tui.py проверяет warning.retry при 502 перед успешной сводкой.

Единые меню: `agent/tui_commands.py`, `agent/tui_backend.py`, `tui/src/menus.ts` и `tui/src/view.ts`
→ `tests/test_tui.py`, `tui/src/view.test.ts` → [TUI](tui.md).
`test_every_catalog_command_opens_selection_without_model_or_credentials` открывает каждую
команду и справку каждого инструмента без модели/ключа и проверяет интерактивный selection;
`test_command_menu_validates_selection_and_executes_only_after_choice` проверяет отложенное
создание сессии, поддельный пункт и устаревшую сессию.
`test_provider_section_pages_add_edit_and_remove` проходит страницы раздела «Провайдеры»:
добавление с готовым способом авторизации, замена Base URL и токена по отдельности и удаление
с подтверждением.
`test_provider_section_navigation_back_reopens_window_and_rejects_stale_route` проверяет
возврат по маршруту: прежнее окно открывается снова, а неизвестный маршрут отвергается.

Запуск TUI: `tests/test_tui.py::test_launcher_refreshes_sources_without_reinstall` проверяет,
что установленный кэш переиспользуется после правки файлов интерфейса и обновляется без сети;
`test_launcher_does_not_check_bun_version` проверяет, что версия Bun не проверяется, а ошибка
называет настоящую причину — нет Bun или не выполнен `gwagent tui install`. Кадры OpenTUI проверяют Enter при
обязательных аргументах, `/providers` и `/permissions`, общий заголовок, отключённые пункты,
пустое окно и восстановление черновика по Esc.

`tests/test_model_limits.py` проверяет резервное окно 120000, вычет токенов ответа и сохранение меньшего реального окна API (32768), а также приоритет известных моделей и ручных ограничений.

Лимит ответа: `tests/test_model_limits.py::test_output_limit_off_omits_max_tokens_and_asks_for_brief_answer` проверяет, что при `max_output_tokens: 0` поле `max_tokens` не отправляется, а система-промпт просит отвечать кратко; `test_output_limit_set_sends_max_tokens` — что положительный предел возвращается в запрос. `test_model_limits_are_individual_and_manual_cap_is_separate` проверяет `output_unlimited` и вход, равный всему окну при снятом пределе.

История ввода и её устаревшие ответы проверяются в
`tui/src/input-history.test.ts`, фоновый режим и дедупликация уведомлений —
в `tui/src/notifications.test.ts` с подставным адаптером без реальных звуков.
JSONL истории и редактирования проверяет `tests/test_tui.py`.

Регрессия max_turn_tokens: tests/test_model_limits.py проверяет отсутствие скрытого
бюджета для длинного цикла и сводки после большого расхода, а также приоритет профиля.
Явный положительный лимит сохраняется в tests/test_compaction.py.

Названия и статистику покрывает tests/test_statistics.py: восстановление из истории,
сохранение ручного имени, изоляция владельцев, отсутствие двойного учёта usage,
ошибки/повторы и доступ инструмента statistics в plan/read_only. tests/test_tui.py
проверяет меню статистики и переименование без модели, tui/src/view.test.ts — группы,
названия и метрики списка сессий. Изменение runtime-реестра проверяется также тестами прав.

Исследовательский Plan проверяется tests/test_plan_roles.py: видимость ролей, запрет
записи explorer/reviewer из обоих режимов, защита от смены роли хуком, фильтрация
служебных сессий, конкретное действие после inspect, HEAD против локального HTTP-сервера
и сохранение SSRF/workspace ограничений. Совместимость worker, отмену, timeout и общий
бюджет продолжают проверять tests/test_tracing_reasoning.py и tests/test_agent_api.py.

Переход после Plan проверяется tui/src/view.test.ts: переключается только режим,
черновик сохраняется, вопрос и смена сессии скрывают действия. tests/test_plan_roles.py
проверяет инструкции о переходе и уточнениях; ожидание ответа question и продолжение
того же цикла покрыты tests/test_agent_modes.py.

tests/test_retry_cleanup.py воспроизводит RemoteProtocolError после текста/reasoning,
обрыв Content-Length настоящим локальным HTTP-сервером, незавершённый SSE и длинную
первую попытку. Проверяется отсутствие повторного выполнения завершённого инструмента
и сохранения неподтверждённых вызовов. TUI и tests/test_chat.mjs проверяют очистку фрагмента.

Релизный smoke: tests/smoke_installed.py --tui запускается вне checkout после установки
wheel. Проверяет четыре CLI --version, создание/отзыв токена, отсутствие настоящего ключа
в list, конфиг, отказ запроса без учётных данных, ресурсы OpenTUI и чата, /live, /health
и OpenAPI. Временное GWAGENT_HOME изолирует пользовательские данные, реальные API не вызываются.
Флаг --tui проверяет чистую установку Bun-пакетов и запуск установленного TUI в PTY.
tests/test_token_cli.py проверяет версии, токены, дубликат, профиль и отзыв.

PTY-smoke дожидается готовности Python-бэкенда, а не только первого кадра.
Нативный вывод собирается по координатам изменённых ячеек; этот разбор проверяется
отдельной регрессией, чтобы оптимизация терминального вывода не давала ложных отказов.
