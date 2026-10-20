"""Пункты меню TUI; выполнение начинается только после выбора пользователя."""
import json
import shlex


SECTIONS = (
    ('Модели', '/models', 'Выбор модели среди всех подключений', ['model', 'модель']),
    ('Провайдеры', '/providers', 'Посмотреть, добавить, изменить или удалить', ['provider', 'connect', 'подключение']),
    ('Сессии', '/sessions', 'Продолжить, создать, экспортировать или удалить', ['session', 'resume', 'new', 'export', 'delete']),
    ('Разрешения', '/permissions', 'Полный доступ, подтверждения или только чтение', ['доступ', 'права']),
    ('Агенты', '/mode', 'Build, Plan и отдельная задача', ['agent', 'build', 'plan', 'сабагент']),
    ('Инструменты', '/tools', 'Посмотреть инструменты и их параметры', ['tool']),
    ('MCP', '/mcp', 'Состояние серверов и доступные инструменты', ['серверы']),
    ('Контекст', '/compact', 'Заполнение окна и создание сводки', ['context', 'сжатие']),
    ('История', '/revert', 'Посмотреть сообщения, изменить или откатить', ['history', 'откат']),
    ('Статистика', '/stats', 'Токены, время, модели, инструменты и ошибки', ['statistics', 'расход', 'метрики']),
    ('Журнал', '/logs', 'Запросы, вызовы модели и инструменты', ['log', 'логи']),
    ('Настройки и данные', '/data', 'Расположение конфигурации и данных', ['settings', 'config', 'настройки']),
    ('Выход', '/exit', 'Закончить работу и сохранить сессию', ['quit', 'q']),
)


def navigation_catalog():
    return [{'kind': 'command', 'label': label, 'command': command, 'description': description,
             'aliases': aliases, 'arguments': 'none'} for label, command, description, aliases in SECTIONS]


NATIVE_PAGES = {'/_ui/sessions/view': '/sessions', '/_ui/sessions/remove': '/delete',
                '/_ui/history/edit': '/revert'}


def provider_page(app, text):
    if text in ('/provider', '/providers', '/connect'):
        return 'Провайдеры', [item(label, '', page='/_ui/providers/' + action)
                              for label, action in (('Посмотреть', 'view'), ('Добавить', 'add'),
                                                    ('Изменить', 'edit'), ('Удалить', 'remove'))]
    if text.startswith('/_ui/providers/'):
        action = text.removeprefix('/_ui/providers/')
        if action == 'exchange':
            return 'Обмен настройками', [item(label, '/provider ' + operation, return_to='/_ui/providers/view')
                for label, operation in (('Импортировать', 'import'), ('Экспортировать', 'export'), ('Обновить список', 'reload'))]
        if action == 'add':
            return 'Добавить провайдера', [item('Base URL и токен', '', operation='provider_add', auth_kind='bearer'),
                                           item('Base URL и сертификат', '', operation='provider_add', auth_kind='mtls')]
        if action not in ('view', 'edit', 'remove'):
            return None
        entries = []
        for name, provider in app.providers.config['providers'].items():
            protected = action == 'remove' and name in (app.provider, app.providers.config['default'])
            entry = item(name, '', provider['url'] + (' · сначала выберите другое подключение' if protected else ''), disabled=protected)
            entry.update({'command': shlex.join(['/provider', 'remove', name]), 'return_to': '/_ui/providers/remove'}
                         if action == 'remove' else {'page': shlex.join(['/_ui/provider/' + action, name])})
            entries.append(entry)
        if action == 'view':
            entries.append(item('Шлюз из настроек', '/provider use @gateway', app.settings['gateway']['url']))
            entries.append(item('Импорт / экспорт и обновление', '', page='/_ui/providers/exchange'))
        return {'view': 'Подключения', 'edit': 'Изменить провайдера', 'remove': 'Удалить провайдера'}[action], entries
    if text.startswith('/_ui/provider/'):
        fields = shlex.split(text)
        if len(fields) != 2:
            return None
        page, name = fields
        provider = app.providers.get(name)
        if page == '/_ui/provider/edit':
            return 'Изменить · ' + name, [item(label, '', provider['url'] if field == 'url' else 'Ввод скрыт',
                operation='provider_edit', provider_name=name, field=field,
                return_to=shlex.join(['/_ui/provider/edit', name]))
                for label, field in (('Заменить Base URL', 'url'), ('Заменить токен', 'token'))]
        if page == '/_ui/provider/view':
            info = f"Base URL: {provider['url']}\nМодель: {provider['model'] or 'не выбрана'}\nСостояние: " + ('включён' if provider.get('enabled', True) else 'выключен')
            return name, [item('Сведения', '', provider['url'], detail=info),
                          item('Использовать', shlex.join(['/provider', 'use', name]), disabled=not provider.get('enabled', True)),
                          item('Дополнительно', '', page=shlex.join(['/_ui/provider/advanced', name]))]
        if page == '/_ui/provider/advanced':
            return 'Дополнительно · ' + name, [item(label, shlex.join(['/provider', action, name]),
                return_to=shlex.join(['/_ui/provider/view', name]),
                disabled=action in ('disable',) and name in (app.provider, app.providers.config['default']))
                for label, action in (('Проверить подключение', 'check'), ('Настроить модели', 'model'),
                    ('Назначить по умолчанию', 'default'),
                    ('Отключить' if provider.get('enabled', True) else 'Включить', 'disable' if provider.get('enabled', True) else 'enable'))]
    return None


def item(label, command, description='', **extra):
    return {'label': label, 'command': command, 'description': description, **extra}


def choices(app, text):
    """None оставляет специализированное окно моделей, сессий или разрешений."""
    page = provider_page(app, text)
    if page is not None:
        return page
    if text == '/sessions':
        return 'Сессии', [item('Посмотреть и продолжить', '', page='/_ui/sessions/view'),
                          item('Создать', '/new'), item('Название текущей сессии', '', page='/rename'), item('Экспортировать', '/export', prompt='Путь к файлу экспорта: '),
                          item('Удалить', '', page='/_ui/sessions/remove')]
    if text == '/rename':
        return 'Название текущей сессии', [item('Переименовать', '/rename', prompt='Название сессии: '),
                                              item('Сгенерировать из истории', '/rename --auto')]
    if text == '/stats':
        return 'Статистика', [item('Текущая сессия', '/stats'), item('Все мои сессии', '/stats all')]
    if text == '/revert':
        return 'История', [item('Посмотреть', '', page='/_ui/history/view'),
                          item('Изменить сообщение или откатить', '', page='/_ui/history/edit'),
                          item('Экспортировать', '/export', prompt='Путь к файлу экспорта: ')]
    if text == '/_ui/history/view':
        return 'Сообщения сессии', [item(f"{index}. {row['message']['role']}", '',
            (row['message'].get('content') or '')[:100], detail=json.dumps(row['message'], ensure_ascii=False, indent=2))
            for index, row in enumerate(app.store.timeline(app.session.id), 1)]
    if text.startswith('/tools '):
        from .runtime import make_registry
        tool = make_registry(app.settings, app.mcp, agent_mode=app.session.agent_mode).get(text[7:].strip())
        if tool is None:
            return None
        required = tool.parameters.get('required', [])
        return 'Инструмент · ' + tool.name, [item('Описание', '', tool.description, detail=tool.description), *[
            item(name, '', ('Обязателен · ' if name in required else '') + value.get('description', ''),
                 detail=json.dumps(value, ensure_ascii=False, indent=2))
            for name, value in tool.parameters.get('properties', {}).items()]]
    if text.startswith('/mcp '):
        name = text[5:].strip()
        server = next((entry for entry in (app.mcp.status(include_disabled=True) if app.mcp else []) if entry['name'] == name), None)
        if server is None:
            return None
        entries = [item(tool, '/tools ' + tool, open_menu=True) for tool in server['tools']] if server['ok'] else []
        if not entries:
            detail = server.get('error') or 'Сервер не предоставил инструментов'
            entries.append(item('Состояние сервера', '', detail, detail=detail))
        return 'MCP · ' + name, entries
    if text == '/mode':
        return 'Агенты', [*[item(label, '/mode ' + mode, description,
                                      tags=['текущий'] if app.session.agent_mode == mode else [])
                                 for mode, label, description in (
                                     ('build', 'Build', 'Выполнение задач и изменение файлов'),
                                     ('plan', 'Plan', 'Исследование и планирование'))],
                          item('Поручить задачу сабагенту', '/agent', prompt='Задача для сабагента: ')]
    if text == '/logs':
        requests = dict.fromkeys(row['request_id'] for row in reversed(app.store.traces(app.session.id)))
        return 'Журнал запросов', [item('Весь журнал сессии', '/logs'), *[
            item(request, '/logs ' + request, 'Модель, инструменты и события этого запроса') for request in requests if request]]
    if text == '/data':
        from .data import paths
        return 'Каталог данных', [item(name, '', path, detail=path) for name, path in paths().items()]
    if text == '/help':
        return 'Главное меню', [item(entry['label'], '', entry['description'], page=entry['command'])
                               for entry in navigation_catalog()]
    if text in ('/connect', '/provider connect'):
        current = app.provider or app.providers.config['default']
        return 'Подключение к провайдеру', [item('Настроить ' + current, shlex.join(['/connect', current])),
            item('Добавить провайдера', '/provider add'), *[
            item(name, shlex.join(['/connect', name]), provider['url'])
            for name, provider in app.providers.config['providers'].items() if name != current]]
    if text.startswith('/provider ') or text.startswith('/providers '):
        fields = text.split()
        if len(fields) != 2:
            return None
        action = fields[1]
        if action in ('use', 'manage', 'edit', 'model', 'check', 'default', 'enable', 'disable', 'remove', 'set'):
            entries = []
            for name, provider in app.providers.config['providers'].items():
                enabled = provider.get('enabled', True)
                protected = name in (app.provider, app.providers.config['default'])
                disabled = (action in ('use', 'default', 'check') and not enabled or
                            action in ('disable', 'remove') and protected or
                            action == 'enable' and enabled or action == 'disable' and not enabled)
                entries.append(item(name, shlex.join(['/provider', 'edit' if action == 'set' else action, name]),
                                    provider['url'], disabled=disabled))
            if action == 'use':
                entries.append(item('Шлюз из настроек', '/provider use @gateway'))
            entries.append(item('Добавить провайдера', '/provider add'))
            titles = {'use': 'Выбор подключения', 'manage': 'Управление подключением', 'edit': 'Настройка подключения',
                      'set': 'Настройка подключения', 'model': 'Настройка моделей', 'check': 'Проверка подключения',
                      'default': 'Провайдер по умолчанию', 'enable': 'Включение провайдера',
                      'disable': 'Отключение провайдера', 'remove': 'Удаление провайдера'}
            return titles[action], entries
        labels = {'add': 'Добавить провайдера', 'import': 'Импортировать из OpenCode',
                  'export': 'Экспортировать в OpenCode', 'reload': 'Перечитать providers.json',
                  'list': 'Показать подключения и пути файлов', 'help': 'Показать справку провайдеров'}
        if action in labels:
            return 'Провайдеры', [item(labels[action], text)]
    simple = {
        '/new': ('Новая сессия', [item('Создать новую сессию', '/new', 'Текущая сессия останется в истории'),
                                 item('Продолжить сохранённую', '/sessions', open_menu=True)]),
        '/compact': ('Контекст', [item('Посмотреть заполнение', '', operation='context_status'),
                                 item('Составить сводку текущей моделью', '/compact', 'Полная история сохранится')]),
        '/export': ('Экспорт сессии', [item('Сохранить полную историю в JSON', '/export', prompt='Путь к файлу экспорта: ')]),
        '/agent': ('Сабагент', [item('Поручить отдельную задачу', '/agent', prompt='Задача для сабагента: ')]),
        '/exit': ('Выход', [item('Выйти из TUI', '/exit', 'Сессия сохранена для продолжения')]),
    }
    return simple.get({'/quit': '/exit', '/q': '/exit'}.get(text, text))
