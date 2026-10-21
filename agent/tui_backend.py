"""Локальный JSONL-мост OpenTUI ↔ существующий App; stdout содержит только события."""

import asyncio
import json
import os
import re
import signal
import sys
import uuid
from datetime import datetime
from pathlib import Path

from . import __version__
from .cli import App, Console, OWNER, parse_args
from .credentials import sanitize
from .runtime import make_registry, make_agent
from .settings import load

PROTOCOL_VERSION = 1
MAX_FRAME = 1048576


class BridgeConsole(Console):
    def __init__(self, bridge):
        super().__init__(as_json=True)
        self.bridge = bridge
        self.color = False

    def c(self, code, text):
        return text

    def line(self, text='', err=False):
        self.bridge.emit({'type': 'notice', 'message': str(text), 'level': 'error' if err else 'info'})

    def end_text(self):
        pass

    def event(self, event):
        if event['type'] == 'done':
            self.bridge.finished = True
        # Сам запрос подтверждения отправляется вместе с регистрацией Future в approve().
        if event['type'] != 'permission_request':
            self.bridge.emit(event)
            if event['type'] in ('tool_result', 'assistant'):
                self.bridge.dashboard()


class Bridge:
    def __init__(self, argv=None, output=None):
        self.args = parse_args(argv)
        if self.args.prompt is not None or self.args.json:
            raise ValueError('OpenTUI не совмещается с -p/--json; используйте --plain')
        self.output = output or sys.stdout
        self.app = None
        self.ready = False
        self.finished = False
        self.work = None
        self.pending = {}
        self.question_ids = set()
        self.approval_ids = set()
        self.requested_mode = None
        self.stop = asyncio.Event()
        self.exit_code = 0
        self.loop = None
        self.offered_models = set()
        self.offered_providers = set()
        self.offered_sessions = {}
        self.selection_sessions = {}
        self.offered_reverts = {}
        self.revert_revision = None
        self.offered_commands = {}
        self.navigation_routes = {}
        self.menu_route = None
        self.replace_navigation = False

    def emit(self, event):
        if event.get('type') == 'table' and event.get('selection') and self.menu_route:
            event['selection']['route'] = self.menu_route
            if self.replace_navigation:
                event['selection']['replace'] = True
                self.replace_navigation = False
            self.navigation_routes[self.menu_route] = self.app.session.id
            if len(self.navigation_routes) > 256:
                self.navigation_routes.pop(next(iter(self.navigation_routes)))
        self.output.write(json.dumps(sanitize(event), ensure_ascii=False, allow_nan=False)+'\n')
        self.output.flush()

    def dashboard_data(self):
        app = self.app
        agent = app.agent or make_agent(llm=None, settings=app.settings, store=app.store, session=app.session, mcp=app.mcp)
        agent.model_metadata = app.get_model_metadata()
        return {'session_id': app.session.id, 'permissions_mode': app.permissions_mode(), 'planning': app.store.planning_state(app.session.id),
                'step': agent.step_state,
                'context': agent.context_status(),
                'mcp': app.mcp.status(include_disabled=True) if app.mcp else [],
                'pending_inputs': app.store.pending_inputs(app.session.id)}

    def dashboard(self):
        if self.ready and self.app and self.app.session:
            self.emit({'type': 'dashboard', **self.dashboard_data()})

    def snapshot(self, history=False):
        from .tui_commands import navigation_catalog
        session = self.app.session
        session.title = self.app.store.ensure_title(session.id)
        event = {'type': 'snapshot', 'protocol': PROTOCOL_VERSION, 'version': __version__,
                 'session': session.to_dict(), 'provider': self.app.provider or 'gateway',
                 'notifications': self.app.settings.get('tui', {}).get('notifications', {}), 'catalog': navigation_catalog(), 'next_agent_mode': self.requested_mode[1] if self.requested_mode and self.requested_mode[0] == session.id else None, **self.dashboard_data()}
        if history:
            editable = {row['seq'] for row in self.app.revert_messages()}
            rows = [{**row['message'], **({'message_id': self.message_id(row), 'content': row['metadata'].get('user_input', row['message'].get('content', ''))} if row['message']['role'] == 'user' and row['seq'] in editable else {}), **({'agent_mode': row['metadata']['agent_mode']} if 'agent_mode' in row['metadata'] else {}), **({'metrics': row['metadata']['metrics']} if row['metadata'].get('metrics') else {})} for row in self.app.store.timeline(session.id)]
            display, size, clipped = [], 0, False
            for row in reversed(rows[-100:]):
                item = json.loads(json.dumps(row))
                if isinstance(item.get('content'), str):
                    clipped |= len(item['content']) > 20000
                    item['content'] = item['content'][:20000]
                if isinstance(item.get('reasoning_content'), str):
                    clipped |= len(item['reasoning_content']) > 20000
                    item['reasoning_content'] = item['reasoning_content'][:20000]
                for call in item.get('tool_calls') or []:
                    fn = call.get('function') or {}
                    if isinstance(fn.get('arguments'), str):
                        clipped |= len(fn['arguments']) > 12000
                        fn['arguments'] = fn['arguments'][:12000]
                encoded = len(json.dumps(item, ensure_ascii=False).encode())
                if size + encoded > MAX_FRAME//2:
                    break
                display.append(item)
                size += encoded
            event['history'] = list(reversed(display))
            event['history_truncated'] = clipped or len(display) < len(rows)
        self.emit(event)

    async def initialize(self):
        try:
            workspace = Path(self.args.cwd or os.getcwd()).expanduser().resolve()
            if not workspace.is_dir():
                raise ValueError('рабочая папка не существует')
            overrides = {'model': self.args.model} if self.args.model else {}
            if self.args.allow_all or self.args.read_only:
                overrides['permissions'] = {'mode': 'allow_all' if self.args.allow_all else 'read_only'}
            settings = load(workspace, extra=self.args.settings, overrides=overrides)
            self.app = App(self.args, settings, workspace, BridgeConsole(self))
            self.app.approve = self.approve
            self.app.question = self.question
            await self.app.start()
            if self.args.model:
                self.app.store.update(self.app.session.id, model=self.args.model)
                self.app.session.model = self.args.model
            self.ready = True
            self.snapshot(history=True)
            self.emit({'type': 'ready', 'protocol': PROTOCOL_VERSION})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.emit({'type': 'fatal', 'message': str(exc)})
            self.exit_code = 2
            self.stop.set()

    async def approve(self, request):
        self.approval_ids.add(request['id'])
        future = asyncio.get_running_loop().create_future()
        self.pending[request['id']] = future
        self.emit(request)
        try:
            return await future
        finally:
            self.pending.pop(request['id'], None)
            self.approval_ids.discard(request['id'])

    async def dialog(self, prompt, secret=False, choices=None, *, question_id=None, agent=None, trace=None):
        identifier = question_id or uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        event = {'type': 'input_request', 'id': identifier, 'prompt': prompt, 'secret': secret}
        if question_id:
            event.update(question=True, allow_custom=True, agent=agent)
            event.update({key: value for key, value in (trace or {}).items() if key in ('request_id', 'span_id')})
        if choices is not None:
            event['choices'] = choices
        elif not question_id and not secret and ('[bearer/basic/none]' in prompt or '[bearer/basic/none/mtls]' in prompt or '[bearer/basic/none/mtls/env]' in prompt):
            event['choices'] = [{'value': 'bearer', 'label': 'Bearer', 'description': 'API-ключ'},
                                {'value': 'basic', 'label': 'Basic', 'description': 'Логин и пароль'},
                                {'value': 'none', 'label': 'Без авторизации'}]
            if 'mtls' in prompt:
                event['choices'].insert(1, {'value': 'mtls', 'label': 'Клиентский сертификат', 'description': 'mTLS: сертификат PEM и приватный ключ'})
            if '[bearer/basic/none/mtls/env]' in prompt:
                event['choices'].append({'value': 'env', 'label': 'Ключ из окружения', 'description': 'Имя переменной с API-ключом'})
        elif not question_id and not secret and '[y/N]' in prompt:
            event['choices'] = [{'value': 'n', 'label': 'Только новые' if prompt.startswith('Объединить') else 'Отмена'},
                                {'value': 'y', 'label': 'Импортировать' if prompt.startswith('Импортировать') else 'Объединить' if prompt.startswith('Объединить') else 'Удалить подключение и ключи' if 'подключение' in prompt else 'Удалить историю', 'danger': True}]
        if not question_id and event.get('choices'):
            default = re.search(r'Enter — (bearer|mtls|basic|none|env)', prompt)
            if default:
                event['default_value'] = default[1]
        self.emit(event)
        try:
            return await future
        finally:
            self.pending.pop(identifier, None)

    async def question(self, request):
        identifier = request['id']
        self.question_ids.add(identifier)
        options = request.get('options', [])
        choices = ([{'label': text, 'value': text} for text in options] +
                   [{'label': 'Написать свой ответ', 'custom': True}]) if options else None
        try:
            return await self.dialog(request['question'], choices=choices, question_id=identifier,
                                     agent=self.app.agent.mode, trace=request)
        finally:
            self.question_ids.discard(identifier)
            self.emit({'type': 'input_closed', 'id': identifier})

    def change_agent(self, mode):
        if mode not in ('build', 'plan'):
            raise ValueError('агент: build | plan')
        if self.work and not self.work.done():
            self.requested_mode = (self.app.session.id, mode) if mode != self.app.session.agent_mode else None
        else:
            self.app.set_agent_mode(mode)
            self.snapshot()
        self.emit({'type': 'agent_mode', 'mode': self.app.session.agent_mode,
                   'next_mode': self.requested_mode[1] if self.requested_mode else None})

    def apply_requested_mode(self):
        if not self.requested_mode or not self.app:
            return
        session_id, mode = self.requested_mode
        self.requested_mode = None
        if self.app.session.id == session_id:
            self.app.set_agent_mode(mode)
        self.snapshot()
        self.emit({'type': 'agent_mode', 'mode': self.app.session.agent_mode, 'next_mode': None})

    def provider_runner(self):
        from .provider_commands import ProviderCommands
        return ProviderCommands(self.app, dialog=self.dialog, menu=self.provider_menu,
            error=lambda message: self.emit({'type': 'error', 'message': message}))

    async def provider_menu(self, title, choices, *, kind='provider-actions'):
        app = self.app
        if kind == 'provider':
            self.offered_providers = {item['provider'] for item in choices if 'provider' in item and item.get('enabled', True)}
            self.selection_sessions['provider'] = app.session.id
        selection = {'kind': kind, 'provider': app.provider, 'session_id': app.session.id, 'choices': choices}
        rows = [[item['label'], item.get('description', ''), 'отключён' if item.get('enabled') is False else ''] for item in choices]
        self.emit({'type': 'table', 'title': title, 'columns': ['Подключение / действие', 'Описание', 'Состояние'],
                   'rows': rows, 'selection': selection})

    async def table_command(self, text):
        app = self.app
        if text == '/data':
            from .data import paths
            self.emit({'type': 'table', 'title': 'Каталог данных', 'columns': ['Файл', 'Путь'], 'rows': [[name, path] for name, path in paths().items()]})
            return True
        if text == '/permissions':
            mode = app.permissions_mode()
            choices = [
                {'key': name, 'label': label, 'description': description,
                 'command': '/permissions ' + name, 'tags': ['текущий'] if mode == name else []}
                for name, label, description in (
                    ('allow_all', 'Полный доступ', 'Все инструменты без обычных подтверждений; явные запреты сохраняются'),
                    ('default', 'С подтверждениями', 'Спрашивать разрешение для изменяющих инструментов'),
                    ('read_only', 'Только чтение', 'Без shell, записи, MCP и делегации'))]
            choices.append({'key': 'save', 'label': 'Сохранить текущий режим по умолчанию',
                            'description': mode + ' → $GWAGENT_HOME/settings.json',
                            'command': '/permissions save'})
            self.emit({'type': 'table', 'title': 'Режим доступа', 'columns': [], 'rows': [],
                       'selection': {'kind': 'permissions', 'session_id': app.session.id,
                                     'current': mode, 'choices': choices}})
            return True
        if text == '/revert':
            messages = app.revert_messages()
            self.revert_revision = app.store.revision(app.session.id)
            self.offered_reverts = {str(row['seq']): row['seq'] for row in messages}
            self.selection_sessions['revert'] = app.session.id
            choices = [{'key': str(row['seq']), 'target': str(row['seq']),
                        'label': row['metadata'].get('user_input', row['message'].get('content') or ''),
                        'description': 'Сообщение №' + str(index), 'group': 'Ваши сообщения'}
                       for index, row in reversed(list(enumerate(messages, 1)))]
            self.emit({'type': 'table', 'title': text, 'columns': [], 'rows': [],
                       'selection': {'kind': 'revert', 'session_id': app.session.id, 'choices': choices}})
            return True
        if text in ('/sessions', '/resume', '/delete'):
            columns = ['ID', 'Название', 'Модель', 'Обновлена']
            sessions = app.store.list(OWNER, limit=200)
            if not any(s.id == app.session.id for s in sessions):
                current = app.store.get(app.session.id)
                if current is not None:
                    sessions.insert(0, current)
            summaries = app.store.session_summaries(OWNER)
            rows = [[s.id, s.title or '(без названия)', s.model, datetime.fromtimestamp(s.updated).strftime('%m-%d %H:%M')]
                    for s in sessions]
            choices = [{'id': s.id, 'title': s.title or '(без названия)', 'model': s.model,
                        'provider': s.profile.removeprefix('provider:') or 'gateway',
                        'workspace': s.workspace, 'updated': s.updated, 'created': s.created,
                        **summaries.get(s.id, {'messages': 0, 'tokens': 0}),
                        'agent_mode': s.agent_mode, 'current': s.id == app.session.id,
                        'enabled': s.id != app.session.id if text == '/delete' else Path(s.workspace).resolve() == app.workspace}
                       for s in sessions]
            selection_kind = 'session-delete' if text == '/delete' else 'session'
            self.offered_sessions[selection_kind] = {c['id'] for c in choices if c['enabled']}
            self.selection_sessions[selection_kind] = app.session.id
        elif text in ('/models', '/model'):
            self.offered_models.clear()
            choices, errors = await app.all_models()
            columns = ['Провайдер', 'Модель', 'Каталог']
            rows = [[c['provider_name'], c['model'],
                     ('API · ' + ', '.join(c['tags'])) if c.get('tags') else ('API' if c['available'] else 'конфиг · API недоступен')]
                    for c in choices]
            self.offered_models = {(c['provider'], c['model']) for c in choices}
            self.selection_sessions['model'] = app.session.id
            for error in errors:
                self.emit({'type': 'warning', 'message': f"{error['provider']}: {error['message']}"})
        elif text == '/tools':
            columns = ['Инструмент', 'Разрешение', 'Описание']
            tools = list(make_registry(app.settings, app.mcp, agent_mode=app.session.agent_mode).tools.values())
            rows = [[t.name,t.default_permission,t.description[:100]] for t in tools]
            choices = [{'key': t.name, 'label': t.name, 'description': t.description,
                        'group': 'MCP' if t.name.startswith('mcp__') else 'Встроенные инструменты',
                        'tags': [t.default_permission], 'command': '/tools ' + t.name} for t in tools]
        elif text.startswith('/tools '):
            tool = make_registry(app.settings, app.mcp, agent_mode=app.session.agent_mode).get(text[7:].strip())
            if tool is None:
                raise ValueError('неизвестный инструмент')
            self.emit({'type': 'notice', 'message': f'{tool.name}: {tool.description}\nРазрешение по умолчанию: {tool.default_permission}. Это справка; инструмент не запускается.'})
            columns = ['Параметр', 'Тип', 'Обязателен', 'Описание']
            schema = tool.parameters
            rows = [[name, json.dumps(value.get('type', '—'), ensure_ascii=False),
                     'да' if name in schema.get('required', []) else 'нет', value.get('description', '')]
                    for name, value in schema.get('properties', {}).items()]
        elif text == '/mcp':
            columns = ['Сервер', 'Состояние', 'Инструменты / ошибка']
            servers = app.mcp.status(include_disabled=True) if app.mcp else []
            def label(server):
                return 'выключен' if server.get('state') == 'disabled' else 'OK' if server['ok'] else 'ошибка'
            def detail(server):
                return ', '.join(server['tools']) if server['ok'] else server.get('error', 'Отключён в настройках')
            rows = [[server['name'], label(server), detail(server)] for server in servers]
            choices = [{'key': server['name'], 'label': server['name'], 'description': detail(server),
                        'tags': [label(server)], 'command': '/mcp ' + server['name']} for server in servers]
        elif text.startswith('/mcp '):
            name = text[5:].strip()
            server = next((s for s in (app.mcp.status(include_disabled=True) if app.mcp else []) if s['name'] == name), None)
            if server is None:
                raise ValueError('неизвестный MCP-сервер')
            self.emit({'type': 'notice', 'message': f"MCP {name}: " + ('OK' if server['ok'] else server.get('error', 'Отключён в настройках'))})
            columns = ['Инструмент']
            rows = [[name] for name in server['tools']] if server['ok'] else []
        else:
            return False
        event = {'type': 'table', 'title': text, 'columns': columns, 'rows': rows}
        if text in ('/models', '/model'):
            event['selection'] = {'kind': 'model', 'current': app.session.model,
                                  'provider': app.provider, 'session_id': app.session.id, 'choices': choices}
        elif text in ('/sessions', '/resume', '/delete'):
            event['selection'] = {'kind': selection_kind, 'current': app.session.id,
                                  'session_id': app.session.id, 'choices': choices}
        elif text in ('/tools', '/mcp'):
            event['selection'] = {'kind': 'tool' if text == '/tools' else 'mcp',
                                  'session_id': app.session.id, 'choices': choices}
        self.emit(event)
        return True

    def command_menu(self, text):
        from .tui_commands import choices
        menu = choices(self.app, text)
        if menu is None:
            return False
        title, entries = menu
        self.offered_commands = {}
        self.selection_sessions['command'] = self.app.session.id
        offered = []
        for entry in entries:
            target = uuid.uuid4().hex
            self.offered_commands[target] = entry
            offered.append({key: value for key, value in entry.items()
                            if key in ('label', 'description', 'group', 'tags', 'disabled')})
            offered[-1].update(key=target, target=target)
        self.emit({'type': 'table', 'title': title, 'columns': [], 'rows': [],
                   'selection': {'kind': 'command', 'title': title, 'session_id': self.app.session.id,
                                 'choices': offered}})
        return True

    @staticmethod
    def message_id(row):
        return row['metadata'].get('input_id') or f"seq:{row['seq']}"

    def history_input(self, message):
        reply = {'type': 'input_history', 'id': message.get('id'), 'session_id': message.get('session_id')}
        try:
            self.validate_message_session(message)
            rows = [{'message_id': self.message_id(row),
                     'text': row['metadata'].get('user_input', row['message'].get('content', ''))}
                    for row in self.app.revert_messages()]
            known = {row['message_id'] for row in rows}
            rows.extend({'message_id': item['id'], 'text': item['text']}
                        for item in self.app.store.pending_inputs(self.app.session.id) if item['id'] not in known)
            cursor = message.get('cursor')
            index = len(rows) if cursor is None else next((i for i, row in enumerate(rows) if row['message_id'] == cursor), None)
            if index is None:
                raise ValueError('история изменилась; начните просмотр заново')
            direction = message.get('direction')
            if direction not in ('previous', 'next'):
                raise ValueError('неверное направление истории')
            index += -1 if direction == 'previous' else 1
            if index < 0:
                reply['boundary'] = 'oldest'
            elif index >= len(rows):
                reply['boundary'] = 'draft'
            else:
                reply.update(rows[index])
                if len(json.dumps(reply, ensure_ascii=False).encode()) > MAX_FRAME//2:
                    raise ValueError('сообщение слишком большое для поля ввода; используйте экспорт')
        except (ValueError, TypeError) as exc:
            reply = {key: reply[key] for key in ('type', 'id', 'session_id')}
            reply['error'] = str(exc)
        self.emit(reply)

    def validate_message_session(self, message):
        if not self.ready or not self.app or message.get('session_id') != self.app.session.id or self.app.session.owner != OWNER:
            raise ValueError('сессия изменилась; повторите действие')

    async def edit_message(self, seq, revision):
        choices = [{'value': 'message', 'label': 'Исправить текст',
                    'description': 'Вернуть текст в поле ввода, убрать последующие ответы из активной истории, сохранить файлы'}]
        try:
            count = self.app.revert_preview(seq)
            choices.append({'value': 'files', 'label': 'Исправить текст и отменить все изменения',
                            'description': f'Отменить все локальные изменения модели с этого сообщения, включая последующие запросы и сабагентов: {count} файлов/папок'})
        except (OSError, ValueError) as exc:
            self.emit({'type': 'notice', 'message': str(exc)})
        mode = await self.dialog('Что сделать с выбранным сообщением? Внешние действия инструментов не отменяются.', choices=choices)
        if mode == 'cancel':
            return
        if mode not in {choice['value'] for choice in choices}:
            raise ValueError('режим отката недоступен')
        result = self.app.apply_revert(seq, revision, files=mode == 'files')
        self.snapshot(history=True)
        self.emit({'type': 'draft', 'text': result['text']})
        self.emit({'type': 'notice', 'message': 'Сообщение возвращено для редактирования' + (f" · восстановлено файлов/папок: {result['files']}" if mode == 'files' else '')})


    async def select(self, message):
        if message['kind'] == 'command':
            entry = self.offered_commands[message['target']]
            return await self.execute(entry['command'], menu_entry=entry)
        try:
            previous = self.app.session.id
            if message['kind'] == 'revert':
                seq = self.offered_reverts[message['target']]
                revision = self.revert_revision
                if self.app.store.revision(self.app.session.id) != revision:
                    raise ValueError('история изменилась; откройте /revert заново')
                await self.edit_message(seq, revision)
                return
            if message['kind'] == 'model':
                await self.app.choose_model(message['provider'], message['model'])
            elif message['kind'] in ('session', 'session-delete'):
                target = self.app.store.get(message['target'])
                if target is None or target.owner != OWNER:
                    raise ValueError('сессия больше не доступна')
                if message['kind'] == 'session':
                    self.app.session = await self.app.activate_session(target)
                    self.app.rebuild()
                elif target.id != self.app.session.id:
                    if (await self.dialog(f'Удалить «{target.title or target.id}»? [y/N]: ')).strip().lower() in ('y', 'да'):
                        self.app.store.delete(target.id)
                        self.emit({'type': 'notice', 'message': 'Сессия удалена'})
            else:
                from .provider_commands import command
                name = message['provider'] if message['provider'] is not None else '@gateway'
                await self.provider_runner().run(command('use', name))
            self.snapshot(history=previous != self.app.session.id)
        except asyncio.CancelledError:
            self.emit({'type': 'done', 'reason': 'cancelled'})
        except Exception as exc:
            self.emit({'type': 'error', 'message': str(exc)})
        finally:
            self.apply_requested_mode()
            self.emit({'type': 'idle'})

    def validate_selection(self, message):
        if message.get('session_id') != self.app.session.id or self.selection_sessions.get(message.get('kind')) != self.app.session.id:
            raise ValueError('список устарел; откройте выбор заново')
        if message.get('kind') == 'command':
            target = message.get('target')
            entry = self.offered_commands.get(target) if isinstance(target, str) else None
            if entry is None or entry.get('disabled'):
                raise ValueError('действие отсутствует в предложенном списке или недоступно')
            return
        if message.get('kind') == 'revert':
            if message.get('target') not in self.offered_reverts:
                raise ValueError('сообщение отсутствует в предложенном списке')
            return
        if message.get('kind') in ('session', 'session-delete'):
            target = message.get('target')
            if not isinstance(target, str) or target not in self.offered_sessions.get(message['kind'], set()):
                raise ValueError('сессия отсутствует в предложенном списке')
            return
        provider = message.get('provider')
        if provider is not None and not isinstance(provider, str):
            raise ValueError('неверный провайдер')
        if message.get('kind') == 'model':
            model = message.get('model')
            if not isinstance(model, str) or (provider, model) not in self.offered_models:
                raise ValueError('модель отсутствует в предложенном списке')
        elif message.get('kind') != 'provider' or provider not in self.offered_providers:
            raise ValueError('провайдер отсутствует в предложенном списке')

    async def execute(self, text, *, menu_entry=None):
        self.finished = False
        try:
            self.menu_route = text
            if menu_entry and menu_entry.get('page'):
                text = menu_entry['page']
                self.menu_route = text
            if menu_entry:
                if menu_entry.get('operation'):
                    await self.ui_operation(menu_entry)
                    return
                if 'detail' in menu_entry:
                    self.emit({'type': 'notice', 'message': menu_entry['label'] + ': ' + menu_entry['detail']})
                    return
                if menu_entry.get('prompt'):
                    value = (await self.dialog(menu_entry['prompt'])).strip()
                    if not value:
                        return
                    text += ' ' + value
            if text.startswith('/'):
                parts = text.split(maxsplit=1)
                cmd, arg = parts[0], parts[1] if len(parts) > 1 else ''
                if (menu_entry is None or menu_entry.get('open_menu') or menu_entry.get('page')) and self.command_menu(text):
                    self.snapshot()
                    return
                from .tui_commands import NATIVE_PAGES
                if await self.table_command(NATIVE_PAGES.get(text, text)):
                    return
                if cmd in ('/provider', '/providers', '/connect'):
                    previous = self.app.session.id
                    await self.provider_runner().run(text)
                    self.snapshot(history=self.app.session.id != previous)
                elif cmd == '/delete':
                    session = self.app.store.find(arg.strip(), owner=OWNER) if arg.strip() else None
                    if session is None or session.id == self.app.session.id:
                        raise ValueError('укажите ID другой сессии; для текущей сначала /new')
                    if (await self.dialog('Удалить историю? [y/N]: ')).lower() in ('y','да'):
                        self.app.store.delete(session.id)
                        self.emit({'type': 'notice', 'message': 'Сессия удалена'})
                else:
                    if cmd == '/agent' and arg.strip():
                        self.emit({'type': 'user', 'text': text})
                    result = await self.app.command(text)
                    if result == 'exit':
                        self.stop.set()
                    else:
                        self.snapshot(history=cmd in ('/new','/resume','/provider'))
                if menu_entry and menu_entry.get('return_to'):
                    self.menu_route = menu_entry['return_to']
                    self.replace_navigation = True
                    self.command_menu(self.menu_route)
            else:
                request_id = uuid.uuid4().hex
                self.emit({'type': 'user', 'text': text, 'message_id': request_id})
                reason = await self.app.ask(text, request_id=request_id)
                if not self.finished:
                    self.emit({'type': 'done', 'reason': reason})
                self.snapshot()
        except asyncio.CancelledError:
            self.emit({'type': 'done', 'reason': 'cancelled'})
        except Exception as exc:
            self.emit({'type': 'error', 'message': str(exc)})
            self.emit({'type': 'done', 'reason': 'error'})
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.cancel()
            self.apply_requested_mode()
            self.emit({'type': 'idle'})

    async def ui_operation(self, entry):
        app, manager = self.app, self.app.providers
        operation = entry['operation']
        if operation == 'context_status':
            self.emit({'type': 'notice', 'message': json.dumps(self.dashboard_data()['context'], ensure_ascii=False, indent=2)})
            return
        if operation == 'provider_add':
            name, candidate, secret = await self.provider_runner().collect(manager.prepare_connection,
                activate=False, auth_kind=entry['auth_kind'], password_input=True)
            manager._save_connection(candidate, name, secret)
            self.emit({'type': 'notice', 'message': 'Провайдер ' + name + ' добавлен'})
            route = '/_ui/providers/view'
        elif operation == 'provider_edit':
            name, field = entry['provider_name'], entry['field']
            provider = manager.get(name)
            value = await self.dialog('Новый Base URL (Enter — сохранить текущий): ' if field == 'url' else 'Новый токен (ввод скрыт): ', secret=field == 'token')
            if field == 'url' and not value.strip():
                value = provider['url']
            candidate, secret = manager.prepare_change(name, field, value)
            changed_endpoint = field == 'url' and candidate['providers'][name]['url'] != provider['url']
            manager._save_connection(candidate, name, secret)
            app.catalogs.pop(name, None)
            if name == app.provider:
                if changed_endpoint:
                    await app.switch_provider(name)
                else:
                    await app.select_provider(name)
                    app.rebuild()
            self.emit({'type': 'notice', 'message': ('Base URL заменён; для нового адреса замените токен' if changed_endpoint and secret.get('type') != 'none' else 'Подключение изменено')})
            from .provider_commands import command
            route = command('edit', name).replace('/provider edit', '/_ui/provider/edit', 1)
        else:
            raise ValueError('Неизвестное действие меню')
        self.snapshot()
        self.menu_route = route
        self.replace_navigation = True
        self.command_menu(route)

    def steer(self, message):
        input_id, text = message.get('id'), message.get('text')
        try:
            if not self.ready or not self.app or not self.app.active_request_id or self.finished or not self.app.task or self.app.task.done():
                raise ValueError('текущий цикл уже завершён; текст возвращён в редактор')
            if not isinstance(text, str) or text.lstrip().startswith('/'):
                raise ValueError('во время работы отправляйте уточнение обычным текстом; команды доступны после завершения')
            item = self.app.store.enqueue_input(self.app.session.id, self.app.active_request_id, input_id, sanitize(text))
            self.app.store.trace(self.app.active_request_id, input_id, '', self.app.session.id,
                                 'input.queued', {'input_id': input_id})
            self.emit({'type': 'input_queued', 'id': input_id, 'text': item['text'], 'status': item['status'],
                       'request_id': self.app.active_request_id})
        except (ValueError, TypeError) as exc:
            self.emit({'type': 'steering_rejected', 'id': input_id, 'text': text if isinstance(text, str) else '', 'message': str(exc)})

    def handle(self, message):
        if not isinstance(message, dict):
            raise ValueError('ожидается объект команды')
        kind = message.get('type')
        if kind == 'shutdown':
            self.stop.set()
        elif kind == 'input_history':
            self.history_input(message)
        elif kind == 'edit_message':
            was_busy = self.work and not self.work.done()
            try:
                self.validate_message_session(message)
                if self.work and not self.work.done():
                    raise ValueError('редактирование доступно после завершения или отмены запроса')
                row = next((row for row in self.app.revert_messages() if self.message_id(row) == message.get('message_id')), None)
                if row is None:
                    raise ValueError('сообщение больше не доступно для редактирования')
                self.offered_reverts = {str(row['seq']): row['seq']}
                self.revert_revision = self.app.store.revision(self.app.session.id)
                self.work = asyncio.create_task(self.select({'kind': 'revert', 'target': str(row['seq'])}))
            except ValueError as exc:
                self.emit({'type': 'notice' if was_busy else 'error', 'level': 'error', 'message': str(exc)})
                if not was_busy:
                    self.emit({'type': 'idle'})
        elif kind == 'steer':
            self.steer(message)
        elif kind == 'set_agent':
            if not self.ready:
                raise ValueError('интерфейс ещё не готов')
            self.change_agent(message.get('mode'))
        elif kind == 'cancel':
            for future in list(self.pending.values()):
                if not future.done():
                    future.cancel()
            if self.app and self.app.task and not self.app.task.done():
                self.app.task.cancel()
            elif self.work and not self.work.done():
                self.work.cancel()
        elif kind in ('approve','answer'):
            identifier = message.get('id')
            if not isinstance(identifier, str):
                raise ValueError('ожидается строковый ID ответа')
            future = self.pending.get(identifier)
            value = message.get('decision') if kind == 'approve' else message.get('value')
            if future is None or future.done() or not (isinstance(value, str) or kind == 'answer' and identifier in self.question_ids and value is None and message.get('cancelled') is True):
                raise ValueError('нет такого ожидающего ответа')
            if kind == 'approve' and identifier not in self.approval_ids or kind == 'answer' and identifier in self.approval_ids:
                raise ValueError('тип ответа не соответствует ожидающему запросу')
            if identifier in self.question_ids and isinstance(value, str) and len(value) > 16000:
                raise ValueError('ответ длиннее 16000 символов')
            if kind == 'approve' and value not in ('allow','deny','always'):
                raise ValueError('неверное решение подтверждения')
            future.set_result(value)
        elif kind in ('submit', 'select', 'navigate'):
            if not self.ready:
                raise ValueError('подключение ещё не готово')
            if self.work and not self.work.done():
                raise ValueError('дождитесь завершения или отмените текущий запрос')
            if kind == 'navigate':
                route = message.get('route')
                session_id = message.get('session_id')
                if not isinstance(route, str) or session_id != self.app.session.id or self.navigation_routes.get(route) != session_id:
                    self.emit({'type': 'error', 'message': 'окно устарело; откройте раздел заново'})
                    self.emit({'type': 'idle'})
                    return
                self.work = asyncio.create_task(self.execute(route))
                return
            if kind == 'select':
                try:
                    self.validate_selection(message)
                except ValueError as exc:
                    self.emit({'type': 'error', 'message': str(exc)})
                    self.emit({'type': 'idle'})
                    return
                self.work = asyncio.create_task(self.select(message))
                return
            text = message.get('text')
            if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_FRAME//2:
                raise ValueError('пустой или слишком большой запрос')
            self.work = asyncio.create_task(self.execute(text.strip()))
        else:
            raise ValueError('неизвестная команда интерфейса')

    async def serve(self):
        self.loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=MAX_FRAME)
        transport, _ = await self.loop.connect_read_pipe(lambda:asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        async def read_commands():
            try:
                while line := await reader.readline():
                    try:
                        self.handle(json.loads(line))
                    except (ValueError, TypeError) as exc:
                        self.emit({'type':'notice','level':'error','message':str(exc)})
            except ValueError:
                self.emit({'type':'fatal','message':'слишком большая команда интерфейса'})
                self.exit_code = 2
            finally:
                self.stop.set()
        async def watch_status():
            previous = None
            while not self.stop.is_set():
                if self.ready and self.app and self.app.mcp:
                    status = self.app.mcp.status(include_disabled=True)
                    if status != previous:
                        self.dashboard()
                        previous = status
                try:
                    await asyncio.wait_for(self.stop.wait(), 2)
                except TimeoutError:
                    pass
        watcher = asyncio.create_task(watch_status())
        pump = asyncio.create_task(read_commands())
        self.work = asyncio.create_task(self.initialize())
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.loop.add_signal_handler(sig, self.stop.set)
        try:
            await self.stop.wait()
        finally:
            for future in list(self.pending.values()):
                if not future.done(): future.cancel()
            if self.work and not self.work.done(): self.work.cancel()
            await asyncio.gather(self.work, return_exceptions=True)
            pump.cancel()
            watcher.cancel()
            await asyncio.gather(pump, watcher, return_exceptions=True)
            transport.close()
            if self.app: await self.app.close()
        return self.exit_code


def main():
    try:
        return asyncio.run(Bridge().serve())
    except (ValueError, OSError) as exc:
        print(json.dumps({'type':'fatal','message':str(exc)},ensure_ascii=False), flush=True)
        return 2


if __name__ == '__main__':
    sys.exit(main())
