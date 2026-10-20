"""Запуск необязательного OpenTUI и установка его зависимостей в пользовательский кэш."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig


def assets():
    for path in (Path(__file__).resolve().parent.parent / 'tui',
                 Path(sysconfig.get_path('data')) / 'share/llm-gateway/tui'):
        if (path / 'src/main.ts').is_file():
            return path
    raise ValueError('не найдены файлы OpenTUI; переустановите llm-gateway-agent')


def bun_binary():
    return shutil.which('bun') or (str(Path.home() / '.bun/bin/bun') if (Path.home() / '.bun/bin/bun').is_file() else None)


def cache_path(source):
    digest = hashlib.sha256()
    for path in sorted([source / 'package.json', source / 'bun.lock', *source.glob('src/*.ts')]):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'gwagent/tui' / digest.hexdigest()[:20]


def installed(source):
    for path in (source, cache_path(source)):
        if (path / 'node_modules/@opentui/core/package.json').is_file():
            return path
    return None


def install():
    source = assets()
    bun = bun_binary()
    if not bun:
        raise ValueError('установите Bun >= 1.3.8: https://bun.sh; затем gwagent tui install')
    target = cache_path(source)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source / 'src', target / 'src', dirs_exist_ok=True)
    for name in ('package.json', 'bun.lock', 'tsconfig.json'):
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
        if argv and argv[0] in ('trust', 'doctor', 'config', 'connect'):
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
        if not bun or not target:
            if args.tui:
                raise ValueError('для OpenTUI нужны Bun >= 1.3.8 и gwagent tui install; доступен gwagent --plain')
            return None
        env = dict(os.environ, GWAGENT_PYTHON=sys.executable)
        parent = str(Path(__file__).resolve().parent.parent)
        env['PYTHONPATH'] = os.pathsep.join(filter(None, [parent, env.get('PYTHONPATH')]))
        forwarded = [a for a in argv if a not in ('--tui', '--plain')]
        return subprocess.run([bun, 'run', str(target / 'src/main.ts'), *forwarded], env=env).returncode
    except (ValueError, OSError) as exc:
        print(f'gwagent: {exc}', file=sys.stderr)
        return 2
