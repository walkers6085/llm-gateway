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
from .storage import digest, file_lock, write_json

DEFAULT_FILE = '~/.config/gwagent/providers.json'
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
                   'tags': TAGS,
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
        self.path = Path(path or DEFAULT_FILE).expanduser()
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

    def connect(self, name, read=input, password=getpass.getpass):
        self.get(name)
        kind = read('Авторизация [bearer/basic/none]: ').strip() or 'bearer'
        if kind not in ('bearer', 'basic', 'none'):
            raise ValueError('ожидается bearer/basic/none')
        secret = {'type': kind, 'endpoint_hash': hashlib.sha256(self.get(name)['url'].rstrip('/').encode()).hexdigest()}
        if kind == 'bearer':
            secret['token'] = password('API-ключ: ')
            if not secret['token']:
                raise ValueError('пустой ключ')
        elif kind == 'basic':
            secret['username'] = read('Логин: ')
            secret['password'] = password('Пароль: ')
        if self.get(name).get('tls', {}).get('key'):
            secret['key_password'] = password('Пароль TLS-ключа (Enter — без пароля): ')
        with file_lock(self.path):
            if digest(self.path) != self._revision:
                raise ValueError('реестр изменился во время ввода; перечитайте подключение')
            self.credentials.set(self.identity(name), secret)
            previous = copy.deepcopy(self.config)
            try:
                self.get(name).update(auth='stored', token_env='')
                self.validate()
                write_json(self.path, self.config)
                self._revision = digest(self.path)
                self._saved = copy.deepcopy(self.config)
            except Exception:
                self.config = previous
                raise

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
    parser.add_argument('--file', default=DEFAULT_FILE)
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
    args = parser.parse_args(argv)
    try:
        manager = ProviderManager(args.file)
        if args.command == 'list':
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
        elif args.command == 'connect': manager.connect(args.name)
        elif args.command in ('enable','disable'): manager.update(args.name, enabled=args.command=='enable')
        else:
            for model in asyncio.run(manager.models(args.name)): print(model)
        return 0
    except (ValueError, OSError, httpx.HTTPError) as exc:
        print('Провайдеры: '+redact(str(exc) if not isinstance(exc,httpx.HTTPError) else type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
