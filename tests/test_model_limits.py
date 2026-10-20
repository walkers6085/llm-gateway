"""Окна из API, настройки и каталог без подмены токенов байтами."""
import io
import json

import httpx
import pytest

from agent.llm import LLMClient
from agent.model_limits import estimate_tokens, metadata, resolve
from agent.settings import DEFAULTS, merge
from tests.test_loop import make_agent, run


@pytest.mark.parametrize('record', [
    {'max_context_length': 128000}, {'context_length': 128000},
    {'context_window': 128000}, {'max_model_len': 128000},
    {'limit': {'context': 128000}}, {'top_provider': {'context_length': 128000}},
])
def test_metadata_formats(record):
    assert metadata(record)['context_window'] == 128000
    assert metadata({'context_length': True, 'max_context_length': '128k'}) == {}
    assert metadata({'context_length': 262144, 'top_provider': {'context_length': 128000}})['context_window'] == 128000


def test_model_limits_are_individual_and_manual_cap_is_separate():
    assert resolve(DEFAULTS, 'glm-4.5-flash')['context_window'] == 131072
    assert resolve(DEFAULTS, 'glm-4.7-flash')['context_window'] == 200000
    api = {'mistral-small-latest': {'context_window': 262144}}
    limits = resolve(DEFAULTS, 'mistral-small-latest', api)
    assert limits['context_source'] == 'api' and limits['max_input_tokens'] == 262144-1024
    assert limits['max_turn_tokens'] > limits['max_input_tokens']
    capped = resolve(merge(DEFAULTS, {'max_input_tokens': 50000}), 'mistral-small-latest', api)
    assert capped['context_window'] == 262144 and capped['max_input_tokens'] == 50000
    assert capped['input_limit_source'] == 'config'
    explicit = merge(DEFAULTS, {'model_profiles': {'alias': {'context_window': 8192, 'max_output_tokens': 512,
                                                           'options': {'api_model': 'mistral-small-latest'}}}})
    assert resolve(explicit, 'alias', api)['context_window'] == 8192
    assert resolve(explicit, 'alias', api)['max_input_tokens'] == 7680
    unknown = resolve(DEFAULTS, 'unknown')
    assert not unknown['context_known'] and unknown['context_source'] == 'fallback'
    assert resolve(merge(DEFAULTS, {'max_input_tokens': 500000}), 'unknown')['max_input_tokens'] == 500000


async def test_catalog_alias_metadata_and_provider_isolation():
    records = [
        {'id': 'mistral-small-actual', 'max_context_length': 262144, 'aliases': ['mistral-small-latest']},
        {'id': 'large', 'context_length': 1048576, 'max_completion_tokens': 8192},
        {'id': 'mistral-small-latest'},
    ]
    async with httpx.AsyncClient(base_url='https://first.test/v1', transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={'data': records}))) as http:
        client = LLMClient(http)
        assert await client.models() == ['mistral-small-actual', 'large', 'mistral-small-latest']
        assert resolve(DEFAULTS, 'mistral-small-latest', client.model_metadata)['context_window'] == 262144
        assert client.model_metadata['large']['max_output_tokens'] == 8192
    async with httpx.AsyncClient(base_url='https://second.test/v1') as http:
        assert not LLMClient(http).model_metadata


async def test_catalog_failure_does_not_prevent_model_request(tmp_path):
    agent, store, _ = make_agent(tmp_path, [{'content': 'Работает'}])
    calls = 0
    async def handler(request):
        nonlocal calls
        if request.method == 'GET':
            calls += 1
            return httpx.Response(403)
        from tests.fakes import sse_body
        return httpx.Response(200, content=sse_body({'content': 'Работает'}), headers={'content-type': 'text/event-stream'})
    previous = agent.llm.http
    agent.llm = LLMClient(httpx.AsyncClient(base_url='http://up/v1', transport=httpx.MockTransport(handler)))
    await agent.llm.discover_model_limits()
    await agent.llm.discover_model_limits()
    assert calls == 1
    assert (await run(agent))[-1]['reason'] == 'stop'
    await previous.aclose()
    await agent.llm.http.aclose()
    store.close()


async def test_provider_metadata_controls_actual_loop_and_context_panel(tmp_path):
    agent, store, _ = make_agent(tmp_path, [{'content': 'Готово'}])
    agent.model_metadata = {'alpha': {'context_window': 200000}}
    context = next(event for event in await run(agent) if event['type'] == 'context')
    assert context['context_window'] == 200000 and context['max_input_tokens'] == 198976
    assert context['context_source'] == 'api'
    status = agent.context_status()
    assert status['context_window'] == 200000
    assert status['estimated_tokens'] < len(json.dumps(store.messages(agent.session.id)).encode()) + 3000
    await agent.llm.http.aclose()
    store.close()


def test_token_estimate_is_not_a_byte_count():
    messages = [{'role': 'user', 'content': 'Привет мир. ' * 1000}]
    size = len(json.dumps([messages, []], ensure_ascii=False).encode())
    assert size / 4 < estimate_tokens(messages) < size / 2


async def test_model_selection_uses_metadata_before_chat_and_keeps_providers_separate(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from agent.tui_backend import Bridge
    from tests.fakes import ServerThread
    monkeypatch.delenv('GW_TOKEN', raising=False)
    upstream = FastAPI()
    @upstream.get('/{provider}/v1/models')
    async def models(provider: str):
        return {'data': [{'id': 'shared', 'max_context_length': 128000 if provider == 'first' else 262144}]}
    with ServerThread(upstream) as server:
        registry = tmp_path / 'providers.json'
        registry.write_text(json.dumps({'default': 'first', 'selected': True, 'providers': {
            name: {'url': f'http://127.0.0.1:{server.port}/{name}/v1', 'token_env': '', 'auth': 'none', 'model': 'shared', 'models': []}
            for name in ('first', 'second')
        }}))
        settings = tmp_path / 'settings.json'
        settings.write_text(json.dumps({'sessions': {'db': str(tmp_path / 's.db')}}))
        bridge = Bridge(['--cwd', str(tmp_path), '--providers-file', str(registry), '--settings', str(settings)], output=io.StringIO())
        try:
            await bridge.initialize()
            assert bridge.ready
            entries, errors = await bridge.app.all_models()
            assert not errors
            assert [entry['context_window'] for entry in entries] == [128000, 262144]
            assert bridge.app.llm is None
            assert bridge.dashboard_data()['context']['context_window'] == 128000
            await bridge.app.choose_model('second', 'shared')
            assert bridge.dashboard_data()['context']['context_window'] == 262144
            assert bridge.dashboard_data()['context']['max_input_tokens'] == 261120
            await bridge.app.model_client()
            assert bridge.app.agent is None
            assert bridge.app.get_model_metadata()['shared']['context_window'] == 262144
        finally:
            if bridge.app:
                await bridge.app.close()


async def test_large_window_is_not_blocked_by_old_turn_budget(tmp_path):
    agent, store, _ = make_agent(tmp_path, [{'content': 'Большой вход принят'}])
    agent.model_metadata = {'alpha': {'context_window': 200000}}
    events = await run(agent, 'word ' * 100000)
    context = next(event for event in events if event['type'] == 'context')
    assert 65536 < context['estimated_tokens'] < context['max_input_tokens']
    assert events[-1]['reason'] == 'stop'
    assert not any(event['type'] == 'warning' for event in events)
    await agent.llm.http.aclose()
    store.close()
