# Обмен настройками с OpenCode

Код: `agent/opencode.py`, `agent/providers.py`. Тесты: `tests/test_opencode.py`,
`tests/test_cli.py`, `tests/test_tui.py`. Источники формата:
[провайдеры OpenCode](https://opencode.ai/docs/providers/),
[модели](https://opencode.ai/docs/models/), [схема](https://opencode.ai/config.json).

providers.json не является opencode.json. У них разные схемы и способ идентификации ключей.
OpenCode использует provider/options/models, а gwagent — providers/url и отдельно
model_profiles/model_names. Секреты gwagent привязаны к ID подключения и хешу URL,
настройки и история находятся в GWAGENT_HOME. Импорт не подключает AI SDK или npm-плагины.

## Команды

В TUI: F3 → «Импорт из OpenCode», «Экспорт в OpenCode», «Параметры моделей NAME».
Также доступны `/provider import`, `/provider export`, `/provider model NAME [MODEL]`.
Импорт спрашивает путь, разрешение объединить совпадающие ID, затем подтверждение
переноса проверенного набора. Экспорт спрашивает провайдера (Enter — все) и новый путь.

Из оболочки:

```bash
# Проверка без записи:
gwproviders import-opencode /path/opencode.jsonc
# Сохранение после проверки, с объединением существующих провайдеров:
gwproviders import-opencode /path/opencode.jsonc --replace --apply
# Экспорт всего совместимого реестра или выбранных провайдеров:
gwproviders export-opencode /path/new-opencode.json
gwproviders export-opencode /path/selected.json --provider corp --provider local
# Пошаговые параметры отдельной модели:
gwproviders model-config corp model-id
```

Без --apply выполняется только проверка. Без --replace совпадение ID останавливает импорт.
При объединении сохраняются стабильный ID, старые модели, метки, модельные параметры,
не затронутые импортом, и ключи других подключений. Исходный файл не меняется. При обычной
ошибке записи реестра прежний auth восстанавливается; это не транзакция двух файлов при сбое питания.

## Поддерживаемый поднабор

| OpenCode | gwagent |
|---|---|
| provider.ID.name | Отображаемое имя подключения |
| options.baseURL | url, обязателен явно |
| options.apiKey: `{env:NAME}` | auth=env, token_env=NAME; значение не копируется |
| options.apiKey: строка | Защищённый auth.json, auth=stored, привязка к URL |
| models.ID.name | model_names.ID |
| models.ID.id | API-ID в model_profiles.ID.options.api_model |
| limit.context / limit.output | max_input_tokens = context − output, max_output_tokens = output |
| options.temperature | temperature_value; реальный temperature в HTTP-теле |
| options.topP | top_p |
| options.reasoningEffort | reasoning_effort |
| cost: input=0, output=0 | Метка free; ненулевая стоимость не поддерживается |
| model: provider/model | Провайдер и модель по умолчанию |
| enabled_providers / disabled_providers | enabled импортируемых подключений |

JSONC поддерживает комментарии и завершающие запятые, URL и строки не повреждаются.
Размер исходного файла ограничен 1 МиБ. Поля проверяются до записи. Контекст в OpenCode
считается общим: для входа резервируется указанный output, иначе прежний модельный лимит
ответа или 1024. Общий бюджет хода gwagent max_turn_tokens импорт не увеличивает:
для больших лимитов ответа его при необходимости настраивают отдельно в settings.json.
Параметры моделей применяются в CLI-агенте, включая сабагентов, независимо для каждого
провайдера. Бенчмарки продолжают задавать параметры запросов своими наборами и настройками.

Поддерживается только явный HTTP chat/completions с npm=@ai-sdk/openai-compatible
(или без npm при явном baseURL). Responses, нативные SDK Anthropic/Google, OAuth,
OpenCode auth.json, file-подстановки, variants, headers, таймауты SDK, small_model,
MCP/агенты/permissions/plugins OpenCode автоматически не переносятся.
Неподдерживаемое поле останавливает импорт, в том числе неизвестные верхние поля;
оно не удаляется и не игнорируется молча. Для такого конфига подготовьте отдельный
файл с поддерживаемой секцией provider и выбором model, остальные настройки оставьте исходной программе.

## Экспорт и сохранность функциональности

Секреты не экспортируются: сохранённый Bearer-ключ заменяется на `{env:GW_ID_API_KEY}`,
имена нужных переменных выводятся после экспорта. Их значения пользователь задаёт отдельно.
Существующий файл назначения не перезаписывается. Экспорт может ограничиваться выбранными
провайдерами: это удобно, если рядом настроен неподдерживаемый mTLS-провайдер.

mTLS, Basic, бюджет хода, нестандартные метки и флаги протокола gwagent не имеют точного
отображения в этом поднаборе. Экспорт такого провайдера останавливается до создания файла,
его native-конфигурация и ключи сохраняются. Сам импорт также не меняет существующий
mTLS-провайдер. Смена URL у существующего stored-подключения требует явно заданного apiKey
или env-ссылки; старый ключ не отправляется новому адресу автоматически.
Экспорт/импорт — не общий файл и не синхронизация авторизации обеих программ.

limit.context также сохраняется как model_profiles.ID.context_window: физическое окно не теряется. Экспорт использует это значение; если дополнительно задан несовместимый отдельный лимит входа, экспорт отклоняется с объяснением, вместо потери ограничения. max_input_tokens=0 означает автоопределение и не экспортируется как нулевое окно.

Команды импорта и экспорта теперь исполняются общим обработчиком провайдеров в CLI и TUI.
Сначала собираются параметры и подтверждение, затем выполняется запись; редактирование
профиля модели через /provider model содержит отдельное поле полного окна context_window.
