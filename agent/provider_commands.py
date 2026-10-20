"""Единый разбор и выполнение команд провайдеров для CLI и OpenTUI."""
import asyncio
import concurrent.futures
import getpass
import shlex
import threading
from dataclasses import dataclass

import httpx

from .credentials import redact
from .llm import LLMClient, LLMError

HELP = ('/provider — подключения; /provider manage [NAME] — управление; '
        'add [NAME], edit [NAME], model [NAME] [MODEL], check [NAME], '
        'default [NAME], enable [NAME], disable [NAME], remove [NAME], '
        'connect [NAME], use [NAME], list, reload, import, export; set NAME URL [MODEL]')
ACTIONS = {'connect': (0, 1), 'manage': (0, 1), 'add': (0, 3), 'edit': (0, 1), 'model': (0, 2),
           'check': (0, 1), 'default': (0, 1), 'enable': (0, 1), 'disable': (0, 1),
           'remove': (0, 1), 'use': (0, 1), 'list': (0, 0), 'help': (0, 0),
           'import': (0, 0), 'export': (0, 0), 'reload': (0, 0), 'set': (2, 3)}


@dataclass(frozen=True)
class Request:
    action: str
    args: tuple[str, ...] = ()


def parse(text):
    try:
        fields = shlex.split(text)
    except ValueError as exc:
        raise ValueError('Незакрытая кавычка в команде провайдера') from exc
    if not fields or fields[0] not in ('/provider', '/providers', '/connect'):
        raise ValueError('Ожидается /provider, /providers или /connect')
    if fields[0] == '/connect':
        if len(fields) > 2:
            raise ValueError('Использование: /connect [NAME]')
        return Request('connect', tuple(fields[1:]))
    if len(fields) == 1:
        return Request('menu')
    action = {'select': 'use', 'delete': 'remove', 'models': 'model'}.get(fields[1], fields[1])
    if action not in ACTIONS:
        if len(fields) == 2:
            return Request('use', (fields[1],))
        raise ValueError('Неизвестное действие провайдера. ' + HELP)
    args = tuple(fields[2:])
    low, high = ACTIONS[action]
    if not low <= len(args) <= high:
        raise ValueError('Неверные аргументы. ' + HELP)
    return Request(action, args)


def command(action, *args):
    return shlex.join(['/provider', action, *args])


class ProviderCommands:
    def __init__(self, app, *, dialog=None, menu=None, error=None):
        self.failed = False
        self.app = app
        self.dialog = dialog or self.plain_dialog
        self.menu = menu or self.plain_menu
        self.error = error or (lambda text: app.console.line(text, err=True))

    async def plain_dialog(self, prompt, secret=False, choices=None):
        if choices:
            for index, item in enumerate(choices, 1):
                self.app.console.line(f'{index}. {item["label"]}' + (' · ' + item['description'] if item.get('description') else ''))
            value = (await asyncio.to_thread(self.app.read, prompt + ' (номер; Enter — отмена): ')).strip()
            if not value:
                raise EOFError
            if {item['value'] for item in choices} == {'n', 'y'}:
                value = {'yes': 'y', 'да': 'y', 'no': 'n', 'нет': 'n'}.get(value.lower(), value.lower())
            if value.isdigit() and 1 <= int(value) <= len(choices):
                return choices[int(value)-1]['value']
            if value not in {item['value'] for item in choices}:
                raise ValueError('Выберите один из предложенных вариантов')
            return value
        return await asyncio.to_thread(getpass.getpass if secret else self.app.read, prompt)

    async def plain_menu(self, title, choices, *, kind='provider-actions'):
        self.app.console.line(title)
        available = [item for item in choices if not item.get('disabled') and item.get('enabled', True)]
        if not available:
            self.app.console.line('Нет доступных подключений')
            return
        options = [{'label': item['label'], 'description': item.get('description', ''),
                    'value': item.get('command') or command('use', item['provider'] if item['provider'] is not None else '@gateway')}
                   for item in available]
        value = await self.dialog('Выберите действие', choices=options)
        await self.run(value)

    def root_choices(self):
        choices = self.app.provider_choices()
        for item in choices:
            item['group'] = 'Подключения'
            if item['provider'] is not None:
                provider = self.app.providers.get(item['provider'])
                item['description'] = provider['url']
                item['tags'] = ['по умолчанию'] if self.app.providers.selected and self.app.providers.config['default'] == item['provider'] else []
        choices.extend([
            {'label': 'Перечитать providers.json', 'command': '/provider reload', 'group': 'Управление'},
            {'label': 'Добавить провайдера', 'command': '/provider add', 'group': 'Управление'},
            {'label': 'Управление подключением', 'command': '/provider manage', 'group': 'Управление'},
            {'label': 'Импорт из OpenCode', 'command': '/provider import', 'group': 'Обмен настройками'},
            {'label': 'Экспорт в OpenCode', 'command': '/provider export', 'group': 'Обмен настройками'},
        ])
        return choices

    async def collect(self, function, *args, **kwargs):
        """Рабочий поток только собирает данные; запись выполняется после успешного await."""
        loop = asyncio.get_running_loop()
        stopped, lock, pending = threading.Event(), threading.Lock(), set()
        def read(prompt, secret=False):
            with lock:
                if stopped.is_set():
                    raise EOFError
                future = asyncio.run_coroutine_threadsafe(self.dialog(prompt, secret=secret), loop)
                pending.add(future)
            try:
                return future.result()
            except concurrent.futures.CancelledError:
                raise EOFError from None
            finally:
                with lock:
                    pending.discard(future)
        try:
            return await asyncio.to_thread(function, *args, read=read,
                **({'password': lambda prompt: read(prompt, True)} if kwargs.pop('password_input', False) else {}), **kwargs)
        finally:
            with lock:
                stopped.set()
                for future in pending:
                    future.cancel()

    async def choose_provider(self, action):
        providers = self.app.providers.config['providers']
        choices = [{'value': name, 'label': name, 'description': provider['url'] + (' · отключён' if not provider.get('enabled', True) else '')}
                   for name, provider in providers.items()
                   if (action not in ('use', 'default', 'check') or provider.get('enabled', True))
                   and (action not in ('disable', 'remove') or name not in (self.app.provider, self.app.providers.config['default']))
                   and (action != 'disable' or provider.get('enabled', True))
                   and (action != 'enable' or not provider.get('enabled', True))]
        if not choices:
            raise ValueError('Нет провайдеров для этого действия; /provider add — добавить')
        label = {'manage': 'управления подключением', 'edit': 'редактирования', 'model': 'настройки моделей',
                 'check': 'проверки', 'default': 'запуска по умолчанию', 'enable': 'включения',
                 'disable': 'отключения', 'remove': 'удаления', 'use': 'работы'}[action]
        return await self.dialog('Выберите провайдера для ' + label, choices=choices)

    async def run(self, text):
        try:
            request = parse(text)
            action, args = request.action, request.args
            app, manager = self.app, self.app.providers
            if app.task and not app.task.done():
                raise ValueError('Дождитесь завершения запроса перед изменением подключения')
            if action == 'help':
                app.console.line(HELP)
                return
            if action == 'menu':
                await self.menu('Провайдеры', self.root_choices(), kind='provider')
                return
            if action == 'list':
                for item in app.provider_choices():
                    app.console.line(f'{item["label"]} · {"включён" if item["enabled"] else "отключён"} · {item["model"] or "модель не выбрана"}')
                app.console.line('Файлы: ' + str(manager.path) + '; ключи: ' + str(manager.credentials.path))
                return
            if action == 'reload':
                from .providers import ProviderManager
                replacement = ProviderManager(manager.path, auth_file=manager.credentials.path)
                current = app.provider
                if current is not None and (current not in replacement.config['providers'] or not replacement.get(current).get('enabled', True)):
                    current = replacement.config['default'] if replacement.get(replacement.config['default']).get('enabled', True) else None
                old = app.connection
                app.providers = replacement
                app.catalogs.clear()
                await app.select_provider(current)
                if old.identity != app.connection.identity or old.endpoint_hash != app.connection.endpoint_hash:
                    app.session = app.create_session(app.settings['model'])
                    if not getattr(app.args, 'management_only', False):
                        app.console.line('Подключение изменилось; создана новая сессия')
                app.rebuild()
                app.console.line('Настройки провайдеров перечитаны с диска')
                return
            if action in ('import', 'export'):
                await self.exchange(action)
                return
            name = args[0] if args else ((app.provider or manager.config['default']) if action == 'connect' else None)
            if action not in ('add', 'connect') and name is None:
                name = await self.choose_provider(action)
            if action == 'manage':
                provider = manager.get(name)
                choices = [{'key': item, 'label': label, 'command': command(item, name)} for item, label in (
                    ('use', 'Выбрать подключение'), ('edit', 'Изменить URL и авторизацию'),
                    ('model', 'Параметры моделей'), ('check', 'Проверить подключение'),
                    ('default', 'Использовать по умолчанию'),
                    ('disable' if provider.get('enabled', True) else 'enable', 'Отключить' if provider.get('enabled', True) else 'Включить'),
                    ('remove', 'Удалить подключение и ключи'))]
                for item in choices:
                    item['description'] = provider['url']
                    item['group'] = name
                    item['disabled'] = (item['key'] in ('disable', 'remove') and name in (app.provider, manager.config['default']) or
                                        item['key'] in ('use', 'default') and not provider.get('enabled', True))
                    if item['disabled']:
                        item['description'] = 'Сначала выберите другое текущее подключение и default' if item['key'] in ('disable', 'remove') else 'Подключение отключено; сначала включите его'
                await self.menu('Подключение ' + name, choices)
            elif action in ('add', 'connect', 'edit'):
                if action == 'add' and len(args) >= 2:
                    manager.add(name, args[1], model=args[2] if len(args) == 3 else '')
                    app.console.line('Подключение ' + name + ' сохранено; /connect ' + name + ' — авторизация')
                    return
                if action == 'edit':
                    manager.get(name)
                elif action == 'add' and name in manager.config['providers']:
                    raise ValueError('Провайдер уже существует; /provider edit ' + name)
                activate = action != 'edit' or name == app.provider
                name, candidate, secret = await self.collect(manager.prepare_connection, name,
                    default_url=app.settings['gateway']['url'] if name == 'gateway' and not manager.path.exists() else None,
                    default_model=app.base_model if name == 'gateway' else '', activate=activate, password_input=True)
                manager._save_connection(candidate, name, secret)
                app.catalogs.pop(name, None)
                if activate:
                    await app.switch_provider(name)
                app.console.line('Подключение сохранено' + (' и выбрано' if activate else '; текущая сессия сохранена'))
            elif action == 'use':
                name = None if name == '@gateway' else name
                candidate = app.provider_info(name)
                if name == app.provider and candidate.identity == app.connection.identity and candidate.endpoint_hash == app.connection.endpoint_hash:
                    if name is not None:
                        manager.use(name)
                    elif manager.selected:
                        manager.config['selected'] = False
                        manager.save()
                    app.console.line('Подключение уже выбрано' + ('' if getattr(app.args, 'management_only', False) else '; текущая сессия сохранена'))
                else:
                    await app.switch_provider(name)
            elif action == 'default':
                if not manager.get(name).get('enabled', True):
                    raise ValueError('Сначала включите подключение')
                manager.use(name)
                app.console.line('Провайдер по умолчанию: ' + name)
            elif action == 'model':
                manager.get(name)
                model = args[1] if len(args) == 2 else None
                if model is None:
                    provider = manager.get(name)
                    cached = app.catalogs.get(name)
                    known = cached[1] if cached and cached[0] == app.provider_info(name).endpoint_hash else {}
                    models = list(dict.fromkeys([*provider.get('models', []), *provider.get('model_profiles', {}), *provider.get('model_names', {}), *known, *([provider['model']] if provider['model'] else [])]))
                    choices = [{'label': model, 'value': 'model:' + model} for model in models]
                    choices.append({'label': 'Указать ID другой модели', 'value': 'new'})
                    selected = await self.dialog('Выберите модель для настройки', choices=choices)
                    model = selected[6:] if selected.startswith('model:') else None
                model, candidate = await self.collect(manager.prepare_model, name, model, include_context=True)
                manager.config = candidate
                manager.save()
                if name == app.provider:
                    app.apply_model_profiles(name)
                    app.rebuild()
                app.console.line('Параметры модели ' + model + ' сохранены')
            elif action in ('enable', 'disable', 'remove'):
                if action != 'enable' and name in (app.provider, manager.config['default']):
                    raise ValueError('Сначала выберите другой провайдер; текущее подключение или default нельзя отключить или удалить')
                if action == 'remove':
                    value = await self.dialog('Удалить подключение ' + name + ' и его сохранённые ключи? [y/N]: ',
                        choices=[{'value': 'n', 'label': 'Отмена'}, {'value': 'y', 'label': 'Удалить подключение и ключи', 'danger': True}])
                    if value.strip().lower() not in ('y', 'да'):
                        app.console.line('Удаление подключения отменено')
                        return
                    manager.remove(name)
                    app.catalogs.pop(name, None)
                else:
                    manager.get(name)
                    manager.update(name, enabled=action == 'enable')
                app.console.line('Подключение ' + name + ': ' + {'enable': 'включено', 'disable': 'отключено', 'remove': 'удалено'}[action])
            elif action == 'set':
                changes = {'url': args[1]}
                if len(args) == 3:
                    changes['model'] = args[2]
                manager.update(name, **changes)
                app.catalogs.pop(name, None)
                if name == app.provider:
                    await app.switch_provider(name)
                app.console.line('Подключение обновлено; /connect ' + name + ' — обновить авторизацию')
            elif action == 'check':
                connection = manager.connection(name)
                async with httpx.AsyncClient(base_url=connection.url.rstrip('/')+'/', timeout=10, trust_env=False, **connection.http_kwargs()) as http:
                    client = LLMClient(http)
                    async with asyncio.timeout(10):
                        models = await client.models()
                app.catalogs[name] = (connection.endpoint_hash, client.model_metadata)
                if name == app.provider and app.llm:
                    app.llm.model_metadata = client.model_metadata
                app.console.line(name + ': подключение работает; моделей: ' + str(len(models)))
        except (EOFError, KeyboardInterrupt):
            self.failed = True
            self.error('Операция с провайдером отменена; изменения не сохранены')
        except TimeoutError:
            self.failed = True
            self.error('Провайдер не ответил вовремя; проверьте URL и доступность API')
        except (ValueError, OSError, LLMError, httpx.HTTPError) as exc:
            self.failed = True
            self.error(redact(str(exc)) or 'Не удалось выполнить команду провайдера')

    async def exchange(self, action):
        app, manager = self.app, self.app.providers
        if action == 'import':
            from .opencode import prepare
            path = (await self.dialog('Путь к opencode.json или opencode.jsonc: ')).strip()
            replace = (await self.dialog('Объединить совпадающие провайдеры? [y/N]: ')).strip().lower() in ('y', 'да')
            candidate, secrets, names = prepare(manager, path, replace=replace)
            if (await self.dialog('Импортировать проверенные подключения ' + ', '.join(names) + '? [y/N]: ')).strip().lower() not in ('y', 'да'):
                return
            manager.save_connections(candidate, secrets)
            app.catalogs.clear()
            await app.switch_provider(manager.config['default'])
            app.console.line('Провайдеры импортированы')
        else:
            from .opencode import export_config
            name = (await self.dialog('Провайдер для экспорта (Enter — все): ')).strip()
            path = (await self.dialog('Путь нового файла экспорта OpenCode: ')).strip()
            variables = export_config(manager, path, providers=[name] if name else None)
            app.console.line('Экспорт создан без секретов' + ('; задайте переменные: ' + ', '.join(variables) if variables else ''))
