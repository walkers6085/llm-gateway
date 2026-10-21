"""Проверка установленного wheel из посторонней папки; без реальных ключей и сети."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
from importlib.metadata import version

import agent
from agent import __version__
from agent.tui import assets
from fastapi.testclient import TestClient
from gateway.app import create_app
from gateway.config import Settings


def terminal_screen(data):
    """Собираем текст экрана: нативный renderer присылает только изменённые ячейки."""
    import re
    screen, row, column = {}, 0, 0
    tokens = re.findall(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\](?:[^\x1b\x07]|\x1b(?!\\))*(?:\x07|\x1b\\)|\x1b.|[^\x1b]+', data.decode('utf-8', 'replace'), re.S)
    for token in tokens:
        if token.startswith('\x1b['):
            code = token[-1]
            if code in ('H', 'f'):
                coordinates = token[2:-1].split(';')
                row = int(coordinates[0] or '1') - 1
                column = int(coordinates[1] or '1') - 1 if len(coordinates) > 1 else 0
            elif code == 'J' and token[2:-1] in ('2', '3'):
                screen.clear()
            continue
        if token.startswith('\x1b'):
            continue
        for char in token:
            if char == '\r':
                column = 0
            elif char == '\n':
                row += 1
            elif ord(char) >= 32:
                screen[row, column] = char
                column += 1
    return '\n'.join(''.join(screen.get((y, x), ' ') for x in range(110)) for y in range(32))


def terminal_smoke(root, env, scripts):
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios
    import time
    subprocess.run([str(scripts / 'gwagent'), 'tui', 'install'], cwd=root, env=env,
                   text=True, capture_output=True, timeout=90, check=True)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
    process = subprocess.Popen([str(scripts / 'gwagent'), '--tui', '--cwd', str(root)],
                               stdin=slave, stdout=slave, stderr=slave, cwd=root,
                               env={**env, 'TERM': 'xterm-256color'}, start_new_session=True)
    os.close(slave)
    output, ready = b'', False
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            if not select.select([master], [], [], .1)[0]:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            output = (output + data)[-1048576:]
            if not ready and 'BUILD' in terminal_screen(output) and 'Готово к запросу' in terminal_screen(output):
                ready = True
                os.write(master, b'\x04')
        assert ready, 'OpenTUI не показал рабочий интерфейс установленного пакета: ' + repr(terminal_screen(output)[-1000:])
        assert process.wait(timeout=10) == 0, 'OpenTUI завершился с ошибкой'
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        os.close(master)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tui', action='store_true', help='установить и проверить OpenTUI в PTY (Linux)')
    args = parser.parse_args()
    assert Path(agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), 'загружены исходники вместо установленного пакета'
    assert version('llm-gateway-agent') == __version__
    with tempfile.TemporaryDirectory(prefix='gwagent-smoke-') as directory:
        root = Path(directory)
        env = {**os.environ, 'GWAGENT_HOME': str(root / 'home')}
        env.pop('PYTHONPATH', None)
        env.pop('GW_TOKEN', None)
        env['DB_PATH'] = str(root / 'tokens.db')
        scripts = Path(sysconfig.get_path('scripts'))
        for command in ('gwagent', 'gwproviders', 'gwbench', 'gwtoken'):
            result = subprocess.run([str(scripts / command), '--version'], cwd=root, env=env,
                                    text=True, capture_output=True, timeout=15, check=True)
            assert __version__ in result.stdout, command
        result = subprocess.run([str(scripts / 'gwagent'), 'config', 'show'], cwd=root, env=env,
                                text=True, capture_output=True, timeout=15, check=True)
        assert json.loads(result.stdout)['values']['max_turn_tokens'] == 0
        result = subprocess.run([str(scripts / 'gwagent'), '-p', 'smoke', '--plain'], cwd=root, env=env,
                                text=True, capture_output=True, timeout=15)
        assert result.returncode != 0, 'запрос без подключения не должен считаться выполненным'
        created = subprocess.run([str(scripts / 'gwtoken'), 'create', 'smoke', '--agent', 'default'], cwd=root, env=env,
                       text=True, capture_output=True, timeout=15, check=True)
        result = subprocess.run([str(scripts / 'gwtoken'), 'list'], cwd=root, env=env,
                                text=True, capture_output=True, timeout=15, check=True)
        assert 'smoke' in result.stdout and created.stdout.strip() not in result.stdout
        subprocess.run([str(scripts / 'gwtoken'), 'revoke', 'smoke'], cwd=root, env=env,
                       text=True, capture_output=True, timeout=15, check=True)
        tui = assets()
        assert tui.is_relative_to(Path(sys.prefix).resolve())
        assert (tui / 'src' / 'notifications.ts').is_file()
        assert (tui / 'bun.lock').is_file()
        if args.tui:
            terminal_smoke(root, env, scripts)
            print('Проверка OpenTUI: чистая установка и запуск в PTY прошли')
        app = create_app(settings=Settings(db_path=str(root / 'gateway.db'), prod=False))
        with TestClient(app) as client:
            assert client.get('/live').status_code == 200
            assert client.get('/health').status_code == 503
            assert client.get('/chat/').status_code == 200
            assert client.get('/openapi.json').json()['info']['version'] == __version__
    print(f'Проверка установленной версии {__version__}: CLI, OpenTUI, чат и API прошли')


if __name__ == '__main__':
    main()
