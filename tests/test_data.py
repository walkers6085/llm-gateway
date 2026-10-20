"""Единое хранилище: изоляция путей, перенос JSON и SQLite с WAL, конфликты и секреты."""

import io
import json
from pathlib import Path
import sqlite3

import pytest

from agent import credentials, data, providers, settings, trust, tui
from agent.cli import amain


def defaults(monkeypatch):
    for module in (credentials, providers, trust):
        monkeypatch.setattr(module, 'DEFAULT_FILE', None)
    monkeypatch.setattr(settings, 'USER_SETTINGS', None)


def test_all_default_paths_follow_one_home_dynamically(tmp_path, monkeypatch):
    defaults(monkeypatch)
    for folder in ('one', 'two'):
        root = tmp_path / folder
        monkeypatch.setenv('GWAGENT_HOME', str(root))
        assert providers.ProviderManager().path == root / 'providers.json'
        assert credentials.Credentials().path == root / 'auth.json'
        assert trust.TrustStore().path == root / 'trust.json'
        root.mkdir();(root / 'settings.json').write_text(json.dumps({'model': folder, 'mcp': {'servers': {'local': {'type': 'stdio', 'command': 'unused'}}}}))
        result = settings.load()
        assert result['model'] == folder and 'local' in result['mcp']['servers']
        assert settings.expand(result['sessions']['db']) == root / 'sessions.db'
        assert settings.expand(result['workspace_root']) == root / 'workspaces'
        assert tui.cache_path(tui.assets()).is_relative_to(root / 'cache/tui')


def test_default_home_and_global_config_is_not_project_config(tmp_path, monkeypatch):
    defaults(monkeypatch)
    monkeypatch.delenv('GWAGENT_HOME')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    assert data.home() == tmp_path / '.gwagent'
    monkeypatch.setenv('GWAGENT_HOME', str(tmp_path / '.gwagent'))
    (tmp_path / '.gwagent').mkdir()
    (tmp_path / '.gwagent/settings.json').write_text('{"model":"global"}')
    assert settings.load(tmp_path)['model'] == 'global'


def test_internal_data_protected_but_managed_workspaces_allowed(tmp_path, monkeypatch):
    root = tmp_path / 'portable'
    monkeypatch.setenv('GWAGENT_HOME', str(root))
    assert credentials.protected(root / 'auth.json')
    assert credentials.protected(root / 'sessions.db-wal')
    assert credentials.protected(root / 'settings.json')
    assert credentials.protected(root / 'cache/file')
    assert not credentials.protected(root / 'workspaces/task/file.py')


def legacy(tmp_path):
    previous = tmp_path / 'old-home'
    config = previous / '.config/gwagent';config.mkdir(parents=True)
    (config / 'providers.json').write_text(json.dumps({'default': 'corp', 'providers': {'corp': {
        'url': 'https://example.test/v1', 'token_env': '', 'auth': 'stored', 'model': '', 'models': [],
        'tls': {'cert': 'client.pem', 'key': 'client.key'}}}}))
    (config / 'auth.json').write_text('{"legacy:corp":{"type":"bearer","token":"migration-test-secret"}}')
    (config / 'auth.json').chmod(0o600)
    (config / 'settings.json').write_text(json.dumps({'sessions': {'db': '~/.local/share/gwagent/sessions.db'}, 'mcp': {'servers': {}}}))
    database = previous / '.local/share/gwagent/sessions.db';database.parent.mkdir(parents=True)
    return previous, config, database


def test_migration_copies_live_wal_and_preserves_paths(tmp_path, monkeypatch):
    root = tmp_path / 'unified';monkeypatch.setenv('GWAGENT_HOME', str(root))
    previous, config, database = legacy(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE records (value TEXT)')
        connection.execute("INSERT INTO records VALUES ('latest')");connection.commit()
        copied = data.migrate(previous)
        assert set(copied) == {'providers.json', 'auth.json', 'settings.json', 'sessions.db'}
        with sqlite3.connect(root / 'sessions.db') as migrated:
            assert migrated.execute('SELECT value FROM records').fetchone()[0] == 'latest'
    assert database.exists() and (config / 'auth.json').exists()
    assert (root / 'auth.json').stat().st_mode & 0o777 == 0o600
    values = json.loads((root / 'providers.json').read_text())
    assert values['providers']['corp']['tls']['cert'] == str(config / 'client.pem')
    assert json.loads((root / 'settings.json').read_text())['sessions']['db'] == '$GWAGENT_HOME/sessions.db'
    before = (root / 'auth.json').read_bytes()
    with pytest.raises(ValueError, match='уже существуют'):
        data.migrate(previous)
    assert (root / 'auth.json').read_bytes() == before


def test_migration_conflicts_or_invalid_json_do_not_publish(tmp_path, monkeypatch):
    root = tmp_path / 'unified';monkeypatch.setenv('GWAGENT_HOME', str(root))
    previous, config, _ = legacy(tmp_path)
    (config / 'settings.json').write_text('{')
    with pytest.raises(ValueError):
        data.migrate(previous)
    assert not (root / 'providers.json').exists() and not (root / 'auth.json').exists()
    (config / 'settings.json').write_text('{}')
    (root / 'auth.json').write_text('existing')
    with pytest.raises(ValueError, match='уже существуют'):
        data.migrate(previous)
    assert (root / 'auth.json').read_text() == 'existing'


async def test_data_command_shows_paths_without_reading_secrets(tmp_path, monkeypatch):
    root = tmp_path / 'portable';monkeypatch.setenv('GWAGENT_HOME', str(root))
    out, err = io.StringIO(), io.StringIO()
    assert await amain(['data'], out=out, err=err) == 0
    assert json.loads(out.getvalue())['home'] == str(root)
    assert not root.exists()



def test_mcp_and_settings_expand_home_without_changing_environment(tmp_path, monkeypatch):
    from agent.mcp import subst
    monkeypatch.delenv('GWAGENT_HOME')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    assert subst({'cwd': '${GWAGENT_HOME}/mcp', 'args': ['$GWAGENT_HOME/cache']}) == {
        'cwd': str(tmp_path / '.gwagent/mcp'), 'args': [str(tmp_path / '.gwagent/cache')]}
    assert settings.expand('$GWAGENT_HOME/sessions.db') == tmp_path / '.gwagent/sessions.db'
    monkeypatch.setenv('GWAGENT_HOME_OTHER', 'untouched')
    assert subst('$GWAGENT_HOME_OTHER') == 'untouched'



def test_migration_rolls_back_publication_and_refuses_unsafe_secrets(tmp_path, monkeypatch):
    root = tmp_path / 'unified';monkeypatch.setenv('GWAGENT_HOME', str(root))
    previous, config, _ = legacy(tmp_path)
    original = data.os.link
    calls = []
    def fail_second(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError('проверка ошибки публикации')
        return original(source, target)
    monkeypatch.setattr(data.os, 'link', fail_second)
    with pytest.raises(OSError):
        data.migrate(previous)
    assert not any((root / name).exists() for name in ('settings.json', 'providers.json', 'auth.json'))
    monkeypatch.setattr(data.os, 'link', original)
    (config / 'auth.json').chmod(0o644)
    with pytest.raises(ValueError, match='0600'):
        data.migrate(previous)
    assert not (root / 'auth.json').exists()
