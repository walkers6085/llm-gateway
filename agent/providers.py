"""Подключения, отдельное хранилище учётных данных и общий TLS-клиент."""

import argparse
import base64
import asyncio
import copy
import getpass
import hashlib
import json
import os
import re
import ssl
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .credentials import Credentials, protect, redact, register
from .schema import validate
from .settings import MODEL_PROFILE
from .data import location
from .storage import digest, file_lock, write_json

DEFAULT_FILE = None
DEFAULT_CONFIG = {'default': 'gateway', 'selected': False, 'providers': {
    'gateway': {'url': 'http://127.0.0.1:7700/v1', 'token_env': 'GW_TOKEN', 'model': '', 'models': []},
    'glm': {'url': 'https://api.z.ai/api/paas/v4', 'token_env': 'ZAI_API_KEY',
            'model': 'glm-5.1', 'models': ['glm-5.1', 'glm-4.5-flash', 'glm-4.7-flash'],
            'tags': {'glm-4.5-flash': ['free'], 'glm-4.7-flash': ['free']}},
    'mistral': {'url': 'https://api.mistral.ai/v1', 'token_env': 'MISTRAL_API_KEY',
                'model': 'mistral-small-latest', 'models': ['mistral-small-latest']}}}
STR = {'type': 'string'}
TAGS = {'type': 'object', 'additionalProperties': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}}}
PROVIDER_SCHEMA = {'type': 'object', 'required': ['url', 'token_env', 'model', 'models'],
    'properties': {'url': STR, 'token_env': STR, 'model': STR, 'id': STR,
                   'enabled': {'type': 'boolean'}, 'auth': {'type': 'string', 'enum': ['none', 'env', 'stored']},
                   'models': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
                   'tags': TAGS, 'name': STR,
                   'model_names': {'type': 'object', 'additionalProperties': STR},
                   'model_profiles': {'type': 'object', 'additionalProperties': MODEL_PROFILE},
                   'options': {'type': 'object', 'properties': {
                       'tools': {'type': 'boolean'}, 'stream': {'type': 'boolean'},
                       'usage': {'type': 'boolean'}, 'temperature': {'type': 'boolean'},
                       'max_tokens_field': {'enum': ['max_tokens', 'max_completion_tokens']}}, 'additionalProperties': False},
                   'tls': {'type': 'object', 'properties': {'cert': STR, 'key': STR, 'ca': STR}, 'additionalProperties': False}},
    'additionalProperties': False}
SCHEMA = {'type': 'object', 'required': ['default', 'providers'], 'properties': {
    'default': STR, 'selected': {'type': 'boolean'},
    'providers': {'type': 'object', 'additionalProperties': PROVIDER_SCHEMA}}, 'additionalProperties': False}


@dataclass
class Connection:
    name: str
    url: str
    model: str
    token: str = field(repr=False, default='')
    identity: str = ''
    auth: object = field(repr=False, default=None)
    tls: dict = field(repr=False, default_factory=dict)
    key_password: str = field(repr=False, default='')
    options: dict = field(default_factory=dict)

    @property
    def endpoint_hash(self):
        return hashlib.sha256(self.url.encode()).hexdigest()

    def http_kwargs(self):
        context = ssl.create_default_context(cafile=self.tls.get('ca') or None)
        if self.tls.get('cert'):
            context.load_cert_chain(self.tls['cert'], self.tls.get('key') or None, self.key_password or None)
        return {'verify': context, 'auth': self.auth,
                'headers': {'authorization': f'Bearer {self.token}'} if self.token else {}}


class ProviderManager:
    def __init__(self, path=None, auth_file=None):
        self.path = Path(path or DEFAULT_FILE or location('providers.json')).expanduser()
        self.credentials = Credentials(auth_file or self.path.with_name('auth.json'))
        self._revision = digest(self.path)
        self.config = json.loads(self.path.read_text()) if self.path.exists() else copy.deepcopy(DEFAULT_CONFIG)
        self.validate()
        self._saved = copy.deepcopy(self.config)

    @property
    def selected(self):
        return self.path.exists() and self.config.get('selected', True)

    def validate(self):
        validate(self.config, SCHEMA)
        providers = self.config['providers']
        if self.config['default'] not in providers:
            raise ValueError('провайдер по умолчанию отсутствует в providers')
        for name, p in providers.items():
            if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
                raise ValueError('неверное имя провайдера')
            url = urlsplit(p['url'])
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError(f'{name}: нужен HTTP(S) URL без логина, пароля, query и fragment')
            _ = url.port
            if p['token_env'] and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', p['token_env']):
                raise ValueError(f'{name}: неверное имя token_env')
            if p.get('tls', {}).get('key') and not p.get('tls', {}).get('cert'):
                raise ValueError('TLS key требует cert')

    def save(self):
        try:
            self.validate()
            with file_lock(self.path):
                if digest(self.path) != self._revision:
                    raise ValueError('реестр изменён другим процессом; перечитайте перед записью')
                write_json(self.path, self.config)
                self._revision = digest(self.path)
            self._saved = copy.deepcopy(self.config)
        except Exception:
            self.config = copy.deepcopy(self._saved)
            raise

    def get(self, name=None):
        name = name or self.config['default']
        if name not in self.config['providers']:
            raise ValueError(f'неизвестный провайдер: {name}')
        return self.config['providers'][name]

    def identity(self, name):
        return self.get(name).get('id') or 'legacy:' + name

    def connection(self, name=None, model=None):
        name = name or self.config['default']
        p = self.get(name)
        if not p.get('enabled', True):
            raise ValueError(f'{name}: подключение отключено')
        identity = self.identity(name)
        mode = p.get('auth', 'env' if p['token_env'] else 'none')
        secret = self.credentials.get(identity) if mode == 'stored' else {}
        if mode == 'stored' and not secret:
            raise ValueError(f'{name}: учётные данные отсутствуют; используйте connect')
        if mode == 'stored' and secret.get('endpoint_hash') != hashlib.sha256(p['url'].rstrip('/').encode()).hexdigest():
            raise ValueError(f'{name}: назначение учётных данных изменилось; выполните connect для нового URL')
        token = os.environ.get(p['token_env'], '') if mode == 'env' else secret.get('token', '')
        if mode == 'env' and not token:
            raise ValueError(f'{name}: задайте переменную {p["token_env"]}')
        register(token)
        auth = httpx.BasicAuth(secret['username'], secret['password']) if secret.get('type') == 'basic' else None
        if auth:
            register('Basic ' + base64.b64encode((secret['username']+':'+secret['password']).encode()).decode())
        tls = {k: str((self.path.parent / Path(v).expanduser()).resolve()) for k, v in p.get('tls', {}).items() if v}
        for path in tls.values():
            protect(path)
        return Connection(name, p['url'].rstrip('/'), model if model is not None else p['model'], token,
                          identity, auth, tls, secret.get('key_password', ''), p.get('options', {}))

    def add(self, name, url, token_env='', model='', models=None, tags=None):
        if name in self.config['providers']:
            raise ValueError(f'провайдер {name} уже существует; используйте update')
        entry = {'id': uuid.uuid4().hex, 'url': url, 'token_env': token_env,
                 'model': model, 'models': models or []}
        if tags:
            entry['tags'] = {key: list(value) for key, value in tags.items()}
        self.config['providers'][name] = entry
        self.save()

    def update(self, name, **changes):
        self.get(name).update(changes)
        self.save()

    def remove(self, name):
        self.get(name)
        if name == self.config['default']:
            raise ValueError('сначала выберите другой провайдер по умолчанию через use')
        identity = self.identity(name)
        del self.config['providers'][name]
        self.save()
        self.credentials.delete(identity)

    def use(self, name, model=None):
        provider = self.get(name)
        self.config['default'], self.config['selected'] = name, True
        if model is not None:
            provider['model'] = model
        self.save()

    def configure(self, name=None, read=input, password=getpass.getpass, *, default_url=None, default_model=''):
        """Собрать подключение целиком до изменения реестра и учётных данных."""
        try:
            adding = name is None
            name = name or read('Имя нового провайдера: ').strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
                raise ValueError('Имя провайдера: латинские буквы, цифры, дефис и подчёркивание')
            previous = self.config['providers'].get(name)
            if adding and previous is not None:
                raise ValueError('Провайдер уже существует; используйте /provider edit ' + name)
            entry = copy.deepcopy(previous or {'id': uuid.uuid4().hex, 'url': '', 'token_env': '',
                                               'model': default_model, 'models': []})
            current_url = default_url or entry['url']
            url = read(f'URL API{f" (Enter — {current_url})" if current_url else " (полный адрес с /v1, если нужен)"}: ').strip() or current_url
            entry['url'] = url.rstrip('/')
            candidate = copy.deepcopy(self.config)
            candidate['providers'][name] = entry
            self._validate_candidate(candidate)
            old_secret = self.credentials.get(self.identity(name)) if previous else {}
            same_endpoint = old_secret.get('endpoint_hash') == hashlib.sha256(entry['url'].encode()).hexdigest()
            default_kind = 'mtls' if entry.get('tls', {}).get('cert') else 'bearer'
            kind = read(f'Способ подключения [bearer/basic/none/mtls] (Enter — {default_kind}): ').strip() or default_kind
            pasted_token = kind if kind.startswith('gw-') else ''
            if pasted_token:
                kind = 'bearer'
            if kind not in ('bearer', 'basic', 'none', 'mtls'):
                raise ValueError('Выберите bearer — API-ключ, mtls — клиентский сертификат, basic — логин/пароль, none — без авторизации')
            secret = {'type': 'none' if kind == 'mtls' else kind,
                      'endpoint_hash': hashlib.sha256(entry['url'].encode()).hexdigest()}
            old_tls = entry.get('tls', {})
            entry.pop('tls', None)
            if kind == 'bearer':
                retain = same_endpoint and old_secret.get('type') == 'bearer'
                token = pasted_token or password('API-ключ / токен gw-… (ввод скрыт' + ('; Enter — сохранить прежний' if retain else '') + '): ')
                secret['token'] = token or (old_secret.get('token', '') if retain else '')
                if not secret['token']:
                    raise ValueError('Пустой ключ; подключение не сохранено')
            elif kind == 'basic':
                secret['username'] = read('Логин: ').strip()
                secret['password'] = password('Пароль (ввод скрыт): ')
                if not secret['username'] or not secret['password']:
                    raise ValueError('Нужны логин и пароль; подключение не сохранено')
            elif kind == 'mtls':
                if urlsplit(entry['url']).scheme != 'https':
                    raise ValueError('Для клиентского сертификата нужен HTTPS URL')
                tls = {}
                for key, label in (('cert', 'Клиентский сертификат PEM'), ('key', 'Приватный ключ PEM (Enter — внутри сертификата)'), ('ca', 'Сертификат CA (Enter — системные CA)')):
                    existing = old_tls.get(key, '')
                    value = read(f'{label}{f" [текущий: {existing}; Enter — сохранить, - — убрать]" if existing else ""}: ').strip()
                    value = '' if value == '-' else value or existing
                    if value:
                        path = (self.path.parent / Path(value).expanduser()).resolve()
                        if not path.is_file():
                            raise ValueError(f'{label}: файл не найден')
                        tls[key] = str(path)
                if not tls.get('cert'):
                    raise ValueError('Нужен файл клиентского сертификата PEM')
                secret['key_password'] = password('Пароль приватного ключа (ввод скрыт; Enter — без пароля): ')
                context = ssl.create_default_context(cafile=tls.get('ca'))
                try:
                    context.load_cert_chain(tls['cert'], tls.get('key'), secret['key_password'] or (lambda: ''))
                except (ssl.SSLError, OSError) as exc:
                    raise ValueError('Не удалось загрузить сертификат и ключ: проверьте PEM, соответствие ключа и пароль') from exc
                entry['tls'] = tls
            candidate['default'], candidate['selected'] = name, True
            entry.update(auth='stored', token_env='', enabled=True)
            self._save_connection(candidate, name, secret)
            return name
        except (EOFError, KeyboardInterrupt):
            raise ValueError('Настройка подключения отменена; изменения не сохранены') from None

    def _validate_candidate(self, candidate):
        previous = self.config
        try:
            self.config = candidate
            self.validate()
        finally:
            self.config = previous

    def _save_connection(self, candidate, name, secret):
        identity = candidate['providers'][name].get('id') or 'legacy:' + name
        self.save_connections(candidate, {identity: secret})

    def save_connections(self, candidate, secrets):
        self._validate_candidate(candidate)
        with file_lock(self.path):
            if digest(self.path) != self._revision:
                raise ValueError('Реестр изменился во время ввода; откройте настройку заново')
            with file_lock(self.credentials.path):
                existed = self.credentials.path.exists()
                previous = self.credentials.read()
                try:
                    if secrets:
                        write_json(self.credentials.path, {**previous, **secrets})
                        self.credentials.read()
                    write_json(self.path, candidate)
                except Exception:
                    if secrets:
                        if existed:
                            write_json(self.credentials.path, previous)
                        else:
                            self.credentials.path.unlink(missing_ok=True)
                    raise
            self.config = candidate
            self._revision = digest(self.path)
            self._saved = copy.deepcopy(candidate)

    def configure_model(self, name, model=None, read=input):
        self.get(name)
        try:
            model = model or read('ID модели (как в API): ').strip()
            if not model or len(model) > 500:
                raise ValueError('Нужен ID модели до 500 символов')
            provider = copy.deepcopy(self.get(name))
            profile = copy.deepcopy(provider.get('model_profiles', {}).get(model, {}))
            label = provider.get('model_names', {}).get(model, model)
            display = read(f'Название модели (Enter — {label}): ').strip() or label
            for key, label in (('max_input_tokens', 'Лимит входного контекста'), ('max_output_tokens', 'Максимум токенов ответа')):
                value = read(f'{label} (Enter — {profile.get(key, "наследовать")}, - — наследовать): ').strip()
                if value == '-':
                    profile.pop(key, None)
                elif value:
                    try:profile[key] = int(value)
                    except ValueError:raise ValueError(label + ': нужно целое число') from None
            options = profile.setdefault('options', {})
            for key, label, convert in (('temperature_value', 'Температура 0–2', float), ('top_p', 'Top-p 0–1', float),
                                         ('reasoning_effort', 'Усилие reasoning: none/minimal/low/medium/high/xhigh', str)):
                value = read(f'{label} (Enter — {options.get(key, "наследовать")}, - — наследовать): ').strip()
                if value == '-':
                    options.pop(key, None)
                elif value:
                    try:options[key] = convert(value)
                    except ValueError:raise ValueError(label + ': неверное числовое значение') from None
            provider.setdefault('model_profiles', {})[model] = profile
            provider.setdefault('model_names', {})[model] = display
            provider['models'] = list(dict.fromkeys([*provider['models'], model]))
            candidate = copy.deepcopy(self.config);candidate['providers'][name] = provider
            self._validate_candidate(candidate)
            self.config = candidate
            self.save()
            return model
        except (EOFError, KeyboardInterrupt):
            raise ValueError('Настройка модели отменена; изменения не сохранены') from None


    def connect(self, name, read=input, password=getpass.getpass):
        try:
            return self._connect(name, read, password)
        except (EOFError, KeyboardInterrupt):
            raise ValueError('Настройка подключения отменена; учётные данные не изменены') from None

    def _connect(self, name, read, password):
        self.get(name)
        kind = read('Тип авторизации [bearer/basic/none] (Enter — bearer, затем скрытый ввод ключа): ').strip() or 'bearer'
        pasted_token = kind if kind.startswith('gw-') else ''
        if pasted_token:
            kind = 'bearer'
        if kind not in ('bearer', 'basic', 'none'):
            raise ValueError('Выберите bearer (API-ключ), basic (логин и пароль) или none; для ключа нажмите Enter, затем введите его в скрытом поле')
        secret = {'type': kind, 'endpoint_hash': hashlib.sha256(self.get(name)['url'].rstrip('/').encode()).hexdigest()}
        if kind == 'bearer':
            secret['token'] = pasted_token or password('API-ключ / токен gw-… (ввод скрыт): ')
            if not secret['token']:
                raise ValueError('пустой ключ')
        elif kind == 'basic':
            secret['username'] = read('Логин: ')
            secret['password'] = password('Пароль: ')
        if self.get(name).get('tls', {}).get('key'):
            secret['key_password'] = password('Пароль TLS-ключа (Enter — без пароля): ')
        candidate = copy.deepcopy(self.config)
        candidate['providers'][name].update(auth='stored', token_env='')
        self._save_connection(candidate, name, secret)

    async def models(self, name=None, transport=None):
        connection = self.connection(name)
        kwargs = connection.http_kwargs()
        async with httpx.AsyncClient(base_url=connection.url + '/', trust_env=False, timeout=30,
                                     transport=transport, **kwargs) as client:
            response = await client.get('models')
            if response.status_code != 200:
                raise ValueError(f'{connection.name}: /models вернул HTTP {response.status_code}')
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                raise ValueError('/models: ожидается объект с массивом data')
            return [m['id'] for m in data['data'] if isinstance(m, dict) and isinstance(m.get('id'), str)]


def parse_tag_args(items):
    """--tag 'MODEL=TAG1,TAG2' (повторяемая) → словарь тегов для update."""
    tags = {}
    for item in items:
        model, sep, tail = item.partition('=')
        model, tail = model.strip(), tail.strip()
        parts = [t.strip() for t in tail.split(',') if t.strip()]
        if not sep or not model or not parts:
            raise ValueError(f'тег: ожидается MODEL=TAG1,TAG2, получено {item!r}')
        tags.setdefault(model, [])
        for tag in parts:
            if tag not in tags[model]:
                tags[model].append(tag)
    return tags


def tags_line(provider):
    tagged = {m: ','.join(t) for m, t in provider.get('tags', {}).items()}
    return ' · ' + '; '.join(f'{m}({t})' for m, t in tagged.items()) if tagged else ''


def main(argv=None):
    parser = argparse.ArgumentParser(description='Менеджер OpenAI-совместимых провайдеров')
    parser.add_argument('--file', default=None, help='реестр провайдеров; по умолчанию GWAGENT_HOME/providers.json')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    for command in ('add', 'update'):
        p = sub.add_parser(command)
        p.add_argument('name')
        p.add_argument('--url', required=command == 'add')
        p.add_argument('--token-env', default='' if command == 'add' else None)
        p.add_argument('--model', default='' if command == 'add' else None)
        p.add_argument('--models', nargs='*', default=[] if command == 'add' else None)
        p.add_argument('--tag', action='append', default=[],
                       help="метки моделей: MODEL=TAG1,TAG2 (повторяемая)")
        p.add_argument('--cert'); p.add_argument('--key'); p.add_argument('--ca')
    for command in ('remove', 'use', 'connect', 'disable', 'enable', 'models'):
        p = sub.add_parser(command)
        p.add_argument('name', nargs='?' if command == 'models' else None)
        if command == 'use': p.add_argument('--model')
    command = sub.add_parser('import-opencode')
    command.add_argument('source')
    command.add_argument('--replace', action='store_true', help='объединить существующие провайдеры')
    command.add_argument('--apply', action='store_true', help='сохранить после проверки; без флага только проверка')
    command = sub.add_parser('export-opencode')
    command.add_argument('output')
    command.add_argument('--provider', action='append', help='выбрать провайдера (можно повторять)')
    command = sub.add_parser('model-config')
    command.add_argument('name');command.add_argument('model', nargs='?')
    args = parser.parse_args(argv)
    try:
        manager = ProviderManager(args.file)
        if args.command == 'import-opencode':
            from .opencode import import_config
            names = import_config(manager,args.source,replace=args.replace,dry_run=not args.apply)
            print(('Импортировано: ' if args.apply else 'Проверка успешна; для записи добавьте --apply: ')+', '.join(names))
        elif args.command == 'export-opencode':
            from .opencode import export_config
            variables = export_config(manager,args.output,providers=args.provider)
            print('Экспорт создан без секретов'+('; задайте переменные: '+', '.join(variables) if variables else ''))
        elif args.command == 'model-config':
            manager.configure_model(args.name,args.model)
            print('Параметры модели сохранены')
        elif args.command == 'list':
            for name, p in manager.config['providers'].items():
                mark = '*' if name == manager.config['default'] and manager.selected else ' '
                print(f"{mark} {name}: {p['url']} · {p['model'] or '(без модели)'} · "
                      f"{'включён' if p.get('enabled', True) else 'отключён'}{tags_line(p)}")
        elif args.command == 'add':
            manager.add(args.name, args.url, args.token_env, args.model, args.models,
                        tags=parse_tag_args(args.tag) if args.tag else None)
            if any((args.cert,args.key,args.ca)):
                manager.update(args.name, tls={k:v for k,v in {'cert':args.cert,'key':args.key,'ca':args.ca}.items() if v})
        elif args.command == 'update':
            changes={k:getattr(args,k) for k in ('url','token_env','model','models') if getattr(args,k) is not None}
            if args.tag:
                fresh = parse_tag_args(args.tag)
                merged = {m: list(t) for m, t in manager.get(args.name).get('tags', {}).items()}
                for m, ts in fresh.items():
                    merged[m] = sorted(set(merged.get(m, []) + ts))
                changes['tags'] = merged
            if any((args.cert,args.key,args.ca)):
                changes['tls']={k:v for k,v in {'cert':args.cert,'key':args.key,'ca':args.ca}.items() if v}
            manager.update(args.name, **changes)
        elif args.command == 'remove': manager.remove(args.name)
        elif args.command == 'use': manager.use(args.name,args.model)
        elif args.command == 'connect': manager.configure(args.name)
        elif args.command in ('enable','disable'): manager.update(args.name, enabled=args.command=='enable')
        else:
            for model in asyncio.run(manager.models(args.name)): print(model)
        return 0
    except (ValueError, OSError, httpx.HTTPError) as exc:
        print('Провайдеры: '+redact(str(exc) if not isinstance(exc,httpx.HTTPError) else type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
