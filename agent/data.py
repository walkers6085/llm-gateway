"""Единый каталог данных агента и безопасное копирование старых хранилищ."""

import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .storage import file_lock, write_json


def home():
    value = os.environ.get('GWAGENT_HOME', '').strip()
    return Path(os.path.expandvars(value)).expanduser().resolve() if value else (Path.home() / '.gwagent').resolve()


def expand_home(value):
    return os.path.expandvars(re.sub(r'\$(?:\{GWAGENT_HOME\}|GWAGENT_HOME\b)', lambda _: str(home()), str(value)))


def location(name):
    return home() / name


def paths():
    return {name: str(location(name)) for name in
            ('settings.json', 'providers.json', 'auth.json', 'trust.json', 'sessions.db', 'cache', 'workspaces', 'bench')}


def migrate(legacy_home=None):
    """Копирование с явным запуском; конфликт не перезаписывается, SQLite включает WAL."""
    old_home = Path(legacy_home).expanduser().resolve() if legacy_home else Path.home()
    config = old_home / '.config/gwagent'
    sources = {name: config / name for name in ('settings.json', 'providers.json', 'auth.json', 'trust.json')}
    sources['sessions.db'] = old_home / '.local/share/gwagent/sessions.db'
    target = home()
    if target == config.resolve() or target == sources['sessions.db'].parent.resolve():
        raise ValueError('Для переноса нужен отдельный каталог, отличный от старого хранилища')
    with file_lock(target / 'migration'):
        planned = {name: source for name, source in sources.items() if source.exists() or source.is_symlink()}
        conflicts = [name for name in planned if location(name).exists() or location(name).is_symlink()]
        if conflicts:
            raise ValueError('Перенос отменён: файлы уже существуют: ' + ', '.join(conflicts))
        staged = {}
        for name, source in planned.items():
            if source.is_symlink() or not source.is_file():
                raise ValueError(f'Перенос отменён: ожидается обычный файл {name}')
            if name == 'sessions.db':
                continue
            if name == 'auth.json' and source.stat().st_mode & 0o077:
                raise ValueError('Старый auth.json требует прав 0600; исправьте их перед переносом')
            value = json.loads(source.read_text())
            if not isinstance(value, dict):
                raise ValueError(f'{name}: ожидается JSON-объект')
            if name == 'providers.json':
                for provider in value.get('providers', {}).values():
                    for key, path in provider.get('tls', {}).items():
                        if path:
                            provider['tls'][key] = str((config / Path(path).expanduser()).resolve())
            elif name == 'settings.json':
                if value.get('sessions', {}).get('db') in ('~/.local/share/gwagent/sessions.db', str(sources['sessions.db'])):
                    value['sessions']['db'] = '$GWAGENT_HOME/sessions.db'
            staged[name] = value
        # Всё готовится во временном каталоге до публикации файлов назначения.
        with tempfile.TemporaryDirectory(prefix='.migration-', dir=target) as temporary:
            temporary = Path(temporary)
            for name, value in staged.items():
                write_json(temporary / name, value)
            if 'sessions.db' in planned:
                source = planned['sessions.db']
                with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as incoming:
                    with sqlite3.connect(temporary / 'sessions.db') as outgoing:
                        incoming.backup(outgoing)
                        if outgoing.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                            raise ValueError('Перенос отменён: база сессий повреждена')
            published = []
            try:
                for name in planned:
                    destination = location(name)
                    # link не заменяет даже появившийся после проверки файл.
                    os.chmod(temporary / name, 0o600)
                    os.link(temporary / name, destination)
                    published.append(destination)
            except Exception:
                for destination in published:
                    destination.unlink()
                raise
        return list(planned)
