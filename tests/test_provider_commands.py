"""Команды провайдеров: один маршрут, атомарная отмена, управление без модели."""
import asyncio
import copy
import io
import json
import threading

import pytest

from agent.cli import App, Console, amain, parse_args
from agent.provider_commands import ProviderCommands, Request, parse
from agent.providers import ProviderManager
from agent.settings import DEFAULTS, merge
from agent import tui


@pytest.mark.parametrize('text, expected', [
    ('/provider', Request('menu')),
    ('/providers  edit', Request('edit')),
    ('/provider\tmodel glm "custom/model name"', Request('model', ('glm', 'custom/model name'))),
    ('/provider add local https://example.test/v1 chat', Request('add', ('local', 'https://example.test/v1', 'chat'))),
    ('/provider glm', Request('use', ('glm',))),
    ('/provider use', Request('use')),
    ('/provider delete local', Request('remove', ('local',))),
    ('/connect', Request('connect')),
    ('/provider connect glm', Request('connect', ('glm',))),
])
def test_parser_routes(text, expected):
    assert parse(text) == expected


@pytest.mark.parametrize('text', ['/provider edit a b', '/provider wat a', '/connect a b', '/provider "oops', '/not-provider a'])
def test_parser_invalid_arguments(text):
    with pytest.raises(ValueError):
        parse(text)


async def app_for(tmp_path):
    args = parse_args(['--cwd', str(tmp_path), '--providers-file', str(tmp_path/'providers.json')])
    out, err = io.StringIO(), io.StringIO()
    settings = merge(DEFAULTS, {'sessions': {'db': str(tmp_path/'s.db')}})
    app = App(args, settings, tmp_path, Console(out, err))
    await app.start()
    return app, out, err


def scripted_dialog(values, seen):
    answers = iter(values)
    async def dialog(prompt, secret=False, choices=None):
        seen.append((prompt, secret, choices))
        return next(answers)
    return dialog


async def test_edit_without_name_keeps_active_session_and_default(tmp_path):
    app, out, _ = await app_for(tmp_path)
    initial = app.session.id
    seen = []
    try:
        runner = ProviderCommands(app, dialog=scripted_dialog(['glm', '', 'env', 'CUSTOM_GLM_KEY'], seen))
        await runner.run('/provider edit')
        assert not runner.failed
        assert seen[0][2] and {item['value'] for item in seen[0][2]} == {'gateway', 'glm', 'mistral'}
        assert app.session.id == initial and app.provider == 'gateway'
        assert app.providers.config['default'] == 'gateway'
        assert app.providers.get('glm')['token_env'] == 'CUSTOM_GLM_KEY'
        assert app.llm is None
    finally:
        await app.close()


async def test_model_without_arguments_selects_provider_model_and_full_context(tmp_path):
    app, _, _ = await app_for(tmp_path)
    seen = []
    try:
        runner = ProviderCommands(app, dialog=scripted_dialog([
            'glm', 'model:glm-4.5-flash', 'Моя модель', '131072', '0', '4096', '.2', '.8', 'low'
        ], seen))
        await runner.run('/provider model')
        assert not runner.failed
        profile = app.providers.get('glm')['model_profiles']['glm-4.5-flash']
        assert profile['context_window'] == 131072 and profile['max_input_tokens'] == 0
        assert profile['max_output_tokens'] == 4096
        assert profile['options'] == {'temperature_value': .2, 'top_p': .8, 'reasoning_effort': 'low'}
        assert app.provider == 'gateway' and app.llm is None
    finally:
        await app.close()


async def test_selection_and_model_selection_persist_provider(tmp_path):
    app, _, _ = await app_for(tmp_path)
    try:
        await ProviderCommands(app).run('/provider use glm')
        assert ProviderManager(app.providers.path).config['default'] == 'glm'
        await app.choose_model('mistral', 'mistral-small-latest')
        assert ProviderManager(app.providers.path).config['default'] == 'mistral'
        assert app.session.model == 'mistral-small-latest'
        await ProviderCommands(app).run('/provider use @gateway')
        assert not ProviderManager(app.providers.path).selected
    finally:
        await app.close()


async def test_cancellation_after_last_input_never_commits(tmp_path):
    app, out, err = await app_for(tmp_path)
    manager = app.providers
    before = copy.deepcopy(manager.config)
    original = manager.prepare_connection
    ready, release, ended = threading.Event(), threading.Event(), threading.Event()
    def held(*args, **kwargs):
        result = original(*args, **kwargs)
        ready.set()
        release.wait(5)
        ended.set()
        return result
    manager.prepare_connection = held
    runner = ProviderCommands(app, dialog=scripted_dialog(['https://example.test/v1', 'none'], []))
    task = asyncio.create_task(runner.run('/provider add cancelled'))
    try:
        assert await asyncio.to_thread(ready.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(ended.wait, 3)
        assert manager.config == before
        assert not manager.path.exists() and not manager.credentials.path.exists()
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await app.close()


async def test_bad_arguments_and_eof_leave_plain_cli_working(tmp_path):
    app, out, err = await app_for(tmp_path)
    def cancelled(prompt):
        raise EOFError
    app.read = cancelled
    try:
        await app.command('/provider edit a b')
        await app.command('/provider add')
        await app.command('/provider list')
        assert 'Неверные аргументы' in err.getvalue()
        assert 'отменена' in err.getvalue()
        assert 'providers.json' in out.getvalue()
    finally:
        await app.close()


async def test_shell_commands_share_handler_without_model_or_bun(tmp_path):
    registry = tmp_path / 'providers.json'
    out, err = io.StringIO(), io.StringIO()
    assert tui.dispatch(['provider', 'list']) is None
    for command in (
        ['add', 'local', 'http://127.0.0.1:1/v1', 'chat'],
        ['default', 'local'], ['use', 'local'], ['list'], ['reload'],
    ):
        assert await amain(['provider', *command, '--cwd', str(tmp_path), '--providers-file', str(registry)], out=out, err=err) == 0, err.getvalue()
    assert ProviderManager(registry).config['default'] == 'local'
    assert 'Подключение local сохранено' in out.getvalue()
    assert await amain(['provider', 'edit', 'a', 'b', '--cwd', str(tmp_path), '--providers-file', str(registry)], out=out, err=err) == 1


async def test_reload_changes_endpoint_without_reusing_history(tmp_path):
    app, _, _ = await app_for(tmp_path)
    try:
        runner = ProviderCommands(app)
        await runner.run('/provider use glm')
        previous = app.session.id
        app.store.add_message(previous, {'role': 'user', 'content': 'Не отправлять новому серверу'})
        config = json.loads(app.providers.path.read_text())
        config['providers']['glm']['url'] = 'https://changed.example.test/v1'
        config['providers']['glm']['model_profiles'] = {'glm-5.1': {'context_window': 150000}}
        app.providers.path.write_text(json.dumps(config))
        await runner.run('/provider reload')
        assert not runner.failed
        assert app.session.id != previous and not app.store.messages(app.session.id)
        assert app.store.messages(previous)[0]['content'] == 'Не отправлять новому серверу'
        assert app.settings['model_profiles']['glm-5.1']['context_window'] == 150000
        assert app.llm is None
    finally:
        await app.close()


async def test_check_uses_catalog_without_inference(tmp_path):
    from fastapi import FastAPI, Request
    from tests.fakes import ServerThread
    upstream, calls = FastAPI(), []
    @upstream.get('/v1/models')
    async def catalog(request: Request):
        calls.append(request.url.path)
        return {'data': [{'id': 'chat', 'max_context_length': 262144}]}
    app, out, _ = await app_for(tmp_path)
    try:
        with ServerThread(upstream) as server:
            app.providers.add('local', f'http://127.0.0.1:{server.port}/v1')
            runner = ProviderCommands(app)
            await runner.run('/provider check local')
            assert not runner.failed
        assert calls == ['/v1/models']
        assert 'подключение работает' in out.getvalue()
        assert app.catalogs['local'][1]['chat']['context_window'] == 262144
        assert app.llm is None
    finally:
        await app.close()


async def test_shell_list_does_not_create_persistent_sessions(tmp_path):
    settings = tmp_path/'settings.json'
    database = tmp_path/'should-not-exist.db'
    settings.write_text(json.dumps({'sessions': {'db': str(database)}}))
    out, err = io.StringIO(), io.StringIO()
    assert await amain(['provider', 'list', '--cwd', str(tmp_path), '--settings', str(settings),
                        '--providers-file', str(tmp_path/'providers.json')], out=out, err=err) == 0
    assert not database.exists()
