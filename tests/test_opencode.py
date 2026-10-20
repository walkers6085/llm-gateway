"""Совместимость OpenCode: перенос поддерживаемых полей, секреты и точные запросы модели."""

import copy
import json

import httpx
import pytest

from agent.opencode import import_config, export_config, read_config
from agent.providers import ProviderManager
from agent.llm import LLMClient


def config(tmp_path, **changes):
    value = {'model': 'custom/chat', 'provider': {'custom': {'npm': '@ai-sdk/openai-compatible', 'name': 'Мой API',
        'options': {'baseURL': 'https://example.test/v1', 'apiKey': 'test-import-secret'},
        'models': {'chat': {'id': 'actual-chat', 'name': 'Моя модель', 'limit': {'context': 8192, 'output': 2048},
                           'options': {'temperature': .3, 'topP': .8, 'reasoningEffort': 'high'}}}}}}
    value.update(changes)
    path = tmp_path / 'opencode.json';path.write_text(json.dumps(value))
    return path, value


def test_import_preview_and_apply_preserve_secrets_and_existing(tmp_path):
    manager = ProviderManager(tmp_path / 'providers.json');path, _ = config(tmp_path)
    assert import_config(manager, path) == ['custom']
    assert not manager.path.exists() and not manager.credentials.path.exists()
    import_config(manager, path, dry_run=False)
    assert {'gateway', 'glm', 'mistral', 'custom'} == set(manager.config['providers'])
    assert manager.connection().token == 'test-import-secret'
    assert manager.get('custom')['model_profiles']['chat']['max_input_tokens'] == 6144
    assert manager.get('custom')['model_profiles']['chat']['options']['api_model'] == 'actual-chat'
    assert 'test-import-secret' not in manager.path.read_text()
    assert manager.credentials.path.stat().st_mode & 0o777 == 0o600
    before = manager.path.read_bytes(), manager.credentials.path.read_bytes()
    with pytest.raises(ValueError, match='уже существует'):
        import_config(manager,path,dry_run=False)
    assert before == (manager.path.read_bytes(),manager.credentials.path.read_bytes())


def test_jsonc_preserves_urls_strings_and_trailing_commas(tmp_path):
    path = tmp_path / 'config.jsonc'
    path.write_text('{/* комментарий */"url":"https://example.test/a//b",// строка\n"s":"/*a*/,}","n":[1,2,],}')
    assert read_config(path) == {'url':'https://example.test/a//b','s':'/*a*/,}','n':[1,2]}
    path.write_text('{/*')
    with pytest.raises(ValueError):read_config(path)


@pytest.mark.parametrize('section,value', [('options', {'baseURL':'https://example.test/v1','headers':{'x-key':'secret'}}),
    ('npm','@ai-sdk/anthropic'), ('models', {'m':{'variants':{'high':{'reasoningEffort':'high'}}}})])
def test_unsupported_options_refuse_before_any_write(tmp_path,section,value):
    path, document = config(tmp_path);document['provider']['custom'][section] = value
    path.write_text(json.dumps(document));manager = ProviderManager(tmp_path/'providers.json')
    with pytest.raises(ValueError):import_config(manager,path,dry_run=False)
    assert not manager.path.exists() and not manager.credentials.path.exists()


def test_env_references_export_roundtrip_and_free_tags(tmp_path,monkeypatch):
    path, document = config(tmp_path)
    document['provider']['custom']['options']['apiKey'] = '{env:CUSTOM_TEST_KEY}'
    document['provider']['custom']['models']['chat']['cost'] = {'input':0,'output':0}
    path.write_text(json.dumps(document));manager = ProviderManager(tmp_path/'providers.json')
    import_config(manager,path,dry_run=False)
    monkeypatch.setenv('CUSTOM_TEST_KEY','dynamic-test-key')
    assert manager.connection().token == 'dynamic-test-key'
    output = tmp_path/'export.json'
    assert export_config(manager,output) == []
    assert 'dynamic-test-key' not in output.read_text() and 'test-import-secret' not in output.read_text()
    other = ProviderManager(tmp_path/'other/providers.json')
    import_config(other,output,replace=True,dry_run=False)
    assert other.get('custom') == manager.get('custom') | {'id':other.get('custom')['id']}
    assert other.get('glm')['tags'] == manager.get('glm')['tags']
    with pytest.raises(ValueError,match='существует'):export_config(manager,output)


def test_literal_key_export_is_placeholder_and_mtls_refuses(tmp_path):
    path,_ = config(tmp_path);manager = ProviderManager(tmp_path/'providers.json')
    import_config(manager,path,dry_run=False);output = tmp_path/'export.json'
    assert export_config(manager,output) == ['GW_CUSTOM_API_KEY']
    assert '{env:GW_CUSTOM_API_KEY}' in output.read_text() and 'test-import-secret' not in output.read_text()
    manager.update('custom',tls={'cert':'some.pem'})
    with pytest.raises(ValueError,match='mTLS'):export_config(manager,tmp_path/'rejected.json')
    assert not (tmp_path/'rejected.json').exists()


async def test_model_options_reach_actual_request(tmp_path):
    path,_ = config(tmp_path);manager = ProviderManager(tmp_path/'providers.json')
    import_config(manager,path,dry_run=False)
    spec = manager.get('custom')['model_profiles']['chat'];seen=[]
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'Ответ'},'finish_reason':'stop'}]})
    async with httpx.AsyncClient(base_url='https://example.test/v1',transport=httpx.MockTransport(handler)) as http:
        client=LLMClient(http,options={**spec['options'],'stream':False},max_tokens=spec['max_output_tokens'])
        assert [e async for e in client.stream('chat',[{'role':'user','content':'Запрос'}])]
    assert seen[0]['model']=='actual-chat' and seen[0]['max_tokens']==2048
    assert seen[0]['temperature']==.3 and seen[0]['top_p']==.8 and seen[0]['reasoning_effort']=='high'


def test_model_wizard_cancel_validation_preserve_credentials_and_metadata(tmp_path):
    manager = ProviderManager(tmp_path/'providers.json')
    manager.connect('gateway',read=lambda _:'bearer',password=lambda _:'unchanged-test-token')
    before = manager.credentials.path.read_bytes()
    answers=iter(['Отображаемое имя','8192','1024','.4','.9','high'])
    manager.configure_model('gateway','chat',read=lambda _:next(answers))
    profile=copy.deepcopy(manager.get('gateway'))
    assert profile['model_names']['chat']=='Отображаемое имя'
    assert profile['model_profiles']['chat']['options']['temperature_value']==.4
    assert manager.credentials.path.read_bytes()==before
    answers=iter(['Новое имя','-1','','','',''])
    with pytest.raises(ValueError):manager.configure_model('gateway','chat',read=lambda _:next(answers))
    assert manager.get('gateway')==profile
    def cancel(_):raise EOFError
    with pytest.raises(ValueError,match='отменена'):manager.configure_model('gateway','chat',read=cancel)
    assert manager.get('gateway')==profile



def test_export_can_select_compatible_provider_without_changing_mtls(tmp_path):
    manager=ProviderManager(tmp_path/'providers.json')
    manager.update('gateway',tls={'cert':'private.pem'})
    before=copy.deepcopy(manager.config)
    output=tmp_path/'selected.json'
    export_config(manager,output,providers=['mistral'])
    assert set(json.loads(output.read_text())['provider'])=={'mistral'}
    assert manager.config==before
