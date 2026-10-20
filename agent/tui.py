"""Запуск необязательного OpenTUI и установка его зависимостей в пользовательский кэш."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

from .data import home, location


def assets():
    for path in (Path(__file__).resolve().parent.parent / 'tui',
                 Path(sysconfig.get_path('data')) / 'share/llm-gateway/tui'):
        if (path / 'src/main.ts').is_file():
            return path
    raise ValueError('не найдены файлы OpenTUI; переустановите llm-gateway-agent')


def bun_binary():
    return shutil.which('bun') or (str(Path.home() / '.bun/bin/bun') if (Path.home() / '.bun/bin/bun').is_file() else None)


def cache_root():
    return location('cache/tui')


def deps_digest(source):
    """Хеш зависимостей: lockfile и список пакетов; версия Bun не проверяется."""
    digest = hashlib.sha256()
    lock = source / 'bun.lock'
    if lock.is_file():
        digest.update(lock.read_bytes())
    manifest = source / 'package.json'
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text())
        except ValueError:
            data = {}
        packages = {key: data.get(key) for key in ('dependencies', 'devDependencies')}
        digest.update(json.dumps(packages, sort_keys=True, ensure_ascii=False).encode())
    return digest.hexdigest()[:20]


def cache_path(source):
    return cache_root() / deps_digest(source)


def installed(source):
    """Каталог с установленными зависимостями; файлы интерфейса обновляются при запуске."""
    wanted = deps_digest(source)
    root = cache_root()
    if not root.is_dir():
        return None
    for path in sorted(root.iterdir()):
        if not path.is_dir() or not (path / 'node_modules/@opentui/core/package.json').is_file():
            continue
        if deps_digest(path) != wanted:
            continue
        return path
    return None


def sync(source, target):
    """Копирует актуальные файлы интерфейса в установленный каталог без обращения к сети."""
    if source.resolve() == target.resolve():
        return
    shutil.copytree(source / 'src', target / 'src', dirs_exist_ok=True)
    for name in ('tsconfig.json',):
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)


def install():
    source = assets()
    bun = bun_binary()
    if not bun:
        raise ValueError('установите Bun: https://bun.sh; затем gwagent tui install')
    home().mkdir(parents=True, exist_ok=True, mode=0o700)
    target = installed(source) or cache_path(source)
    target.mkdir(parents=True, exist_ok=True)
    sync(source, target)
    for name in ('package.json', 'bun.lock'):
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)
    result = subprocess.run([bun, 'install', '--frozen-lockfile', '--ignore-scripts'], cwd=target)
    if result.returncode == 0:
        print('OpenTUI установлен. Запуск: gwagent --tui')
    return result.returncode


def dispatch(argv):
    """None означает обычный Python CLI. Сетевой установки при запуске нет."""
    try:
        if argv[:1] == ['tui']:
            if argv[1:] != ['install']:
                raise ValueError('использование: gwagent tui install')
            return install()
        if argv and argv[0] in ('trust', 'doctor', 'config', 'connect', 'data', 'provider', 'providers'):
            return None
        from .cli import parse_args
        args = parse_args(argv)
        if args.plain:
            return None
        if args.prompt is not None or args.json:
            if args.tui:
                raise ValueError('--tui не совмещается с -p/--json; используйте --plain')
            return None
        tty = sys.stdin.isatty() and sys.stdout.isatty()
        if not tty:
            if args.tui:
                raise ValueError('--tui требует интерактивного терминала')
            return None
        bun = bun_binary()
        source = assets()
        target = installed(source)
        if not bun:
            if args.tui:
                raise ValueError('для OpenTUI нужен Bun: https://bun.sh; доступен gwagent --plain')
            return None
        if not target:
            if args.tui:
                raise ValueError('OpenTUI не установлен: выполните gwagent tui install; доступен gwagent --plain')
            return None
        sync(source, target)
        env = dict(os.environ, GWAGENT_PYTHON=sys.executable)
        parent = str(Path(__file__).resolve().parent.parent)
        env['PYTHONPATH'] = os.pathsep.join(filter(None, [parent, env.get('PYTHONPATH')]))
        forwarded = [a for a in argv if a not in ('--tui', '--plain')]
        return subprocess.run([bun, 'run', str(target / 'src/main.ts'), *forwarded], env=env).returncode
    except (ValueError, OSError) as exc:
        print(f'gwagent: {exc}', file=sys.stderr)
        return 2
