"""Администрирование токенов поставляется в wheel и закрывает SQLite после команды."""
import sys

import pytest

from agent import __version__
from agent.cli import parse_args
from gateway.token_cli import main
from gateway.tokens import TokenStore


def test_cli_versions_exit_before_configuration(capsys, monkeypatch):
    from agent.providers import main as providers_main
    from bench.__main__ import main as bench_main
    for callback in (lambda: parse_args(['--version']), lambda: providers_main(['--version']), main, bench_main):
        monkeypatch.setattr(sys, 'argv', ['cli', '--version'])
        with pytest.raises(SystemExit) as exc:
            callback()
        assert exc.value.code == 0
        assert __version__ in capsys.readouterr().out


def test_token_cli_create_list_permissions_revoke_and_duplicate(tmp_path, monkeypatch, capsys):
    db = tmp_path / 'gateway.db'
    monkeypatch.setenv('DB_PATH', str(db))
    def command(*args):
        monkeypatch.setattr(sys, 'argv', ['gwtoken', *args])
        code = main()
        return code, capsys.readouterr()
    code, output = command('create', 'release', '--agent', 'default')
    assert code == 0
    raw = output.out.strip()
    assert raw.startswith('gw-')
    code, output = command('list')
    assert code == 0 and 'release' in output.out and raw not in output.out
    assert command('create', 'release')[0] == 1
    assert command('agent', 'release', '--off')[0] == 0
    store = TokenStore(str(db))
    try:
        assert store.lookup(raw).agent_profile == ''
    finally:
        store.close()
    assert command('revoke', 'release')[0] == 0
    store = TokenStore(str(db))
    try:
        assert store.lookup(raw).revoked is not None
    finally:
        store.close()


def test_smoke_understands_incremental_terminal_screen():
    from tests.smoke_installed import terminal_screen
    data=('\x1b[1;1HBUILD\x1b[26;2HПодготовка сессии…'
          '\x1b[26;2HГ\x1b[26;4Hтово к запро\x1b[26;17Hу\x1b[26;18H  ').encode()
    assert 'Готово к запросу' not in data.decode()
    assert 'Готово к запросу' in terminal_screen(data)
