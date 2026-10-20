"""Plan исследует фактами; внутренние роли не обходят запрет изменения файлов."""
import json
import pytest

from agent.permissions import Permissions, ALLOW, DENY
from agent.settings import DEFAULTS
from agent.runtime import make_registry
from agent.tools.base import ToolContext, ToolError
from agent.tools.inspection import Inspect
from agent.tools.files import WriteFile, ReadFile
from agent.tools.subagent import Subagent
from tests.test_loop import make_agent, run, hook_cmd
from tests.fakes import tool_call


@pytest.mark.parametrize('role', ['explorer', 'reviewer'])
@pytest.mark.parametrize('mode', ['build', 'plan'])
async def test_safe_roles_in_build_and_plan_cannot_write(tmp_path, role, mode):
    seen = []
    agent, store, _ = make_agent(tmp_path, [
        {'tool_calls': [tool_call('delegate', 'subagent', role=role, task='Проверь доступность README', context='Нужны проверенные факты')]},
        {'tool_calls': [tool_call('illegal', 'write_file', path='forbidden', content='x')]},
        {'content': 'Запись недоступна; требуются результаты чтения'},
        {'content': 'План на основании результатов исследования'},
    ], seen, permissions={'mode': 'allow_all'}, model_retry={'max_retries': 0})
    agent.registry.add(Subagent());agent.registry.add(WriteFile());agent.registry.add(ReadFile())
    store.update(agent.session.id, agent_mode=mode);agent.session = store.get(agent.session.id)
    try:
        events = await run(agent, 'Изучи проект и спланируй')
        assert events[-1]['reason'] == 'stop' and not (tmp_path / 'forbidden').exists()
        child_id = next(e['session_id'] for e in events if e['type'] == 'subagent_start')
        assert store.get(child_id).agent_mode == 'plan'
        assert child_id not in {s.id for s in store.list('local')}
        assert child_id in {s.id for s in store.list('local', include_internal=True)}
        assert 'write_file' not in {t['function']['name'] for t in seen[1]['tools']}
        assert 'subagent' not in {t['function']['name'] for t in seen[1]['tools']}
        assert 'Внутренняя роль: ' + role in seen[1]['messages'][0]['content']
        assert 'Нужны проверенные факты' in seen[1]['messages'][0]['content']
        assert next(e for e in events if e['type'] == 'subagent_start')['role'] == role
        assert 'Осталось делегаций на текущий запрос: 2' in seen[-1]['messages'][0]['content']
        store.delete(agent.session.id)
        assert child_id not in {s.id for s in store.list('local')}
    finally:
        await agent.llm.http.aclose();store.close()


async def test_plan_defaults_to_explorer_but_hook_cannot_enable_worker(tmp_path):
    agent, store, _ = make_agent(tmp_path, [
        {'tool_calls': [tool_call('delegate', 'subagent', task='Исследуй')]},
        {'content': 'План'},
    ], permissions={'mode': 'allow_all'}, hooks={'PreToolUse': [{'command': hook_cmd("print('{\"decision\":\"allow\",\"updated_input\":{\"task\":\"Измени файл\",\"role\":\"worker\"}}')")}]})
    agent.registry.add(Subagent())
    store.update(agent.session.id, agent_mode='plan');agent.session=store.get(agent.session.id)
    try:
        spec=next(t for t in agent.tool_specs() if t['function']['name']=='subagent')
        assert spec['function']['parameters']['properties']['role']['enum'] == ['explorer', 'reviewer']
        events=await run(agent)
        assert not any(e['type']=='subagent_start' for e in events)
        assert not next(e for e in events if e['type']=='tool_result')['ok']
    finally:
        await agent.llm.http.aclose();store.close()


def test_role_permissions_respect_deny_and_read_only():
    tool=Subagent()
    assert Permissions({}).decide(tool, {'role':'explorer'})[0] == ALLOW
    assert Permissions({'deny':['subagent(explorer)']}).decide(tool, {'role':'explorer'})[0] == DENY
    assert Permissions({'mode':'read_only'}).decide(tool, {'role':'worker'}, 'allow')[0] == DENY
    assert Permissions({'mode':'read_only'}).decide(tool, {'role':'reviewer'})[0] == ALLOW
    assert make_registry(DEFAULTS,agent_mode='plan').get('subagent') is not None


async def test_inspect_local_checks_and_workspace_boundary(tmp_path):
    tool=Inspect();ctx=ToolContext(tmp_path, DEFAULTS)
    (tmp_path/'README.md').write_text('Проект')
    assert json.loads(await tool.run({'kind':'path','target':'README.md'},ctx))['type']=='file'
    assert json.loads(await tool.run({'kind':'path','target':'missing'},ctx))['available'] is False
    assert json.loads(await tool.run({'kind':'program','target':'certainly-missing-program-124'},ctx))['available'] is False
    with pytest.raises(ToolError):
        await tool.run({'kind':'path','target':'../outside'},ctx)
    with pytest.raises(ToolError):
        await tool.run({'kind':'program','target':'/bin/sh'},ctx)


async def test_plan_uses_check_result_before_concrete_action(tmp_path):
    seen=[]
    agent,store,_=make_agent(tmp_path,[
        {'tool_calls':[tool_call('check','inspect',kind='path',target='pyproject.toml')]},
        {'content':'pyproject.toml отсутствует. Создать pyproject.toml с метаданными пакета; затем проверить сборку.'},
    ],seen,model_retry={'max_retries':0})
    agent.registry.add(Inspect());agent.registry.add(Subagent());store.update(agent.session.id,agent_mode='plan');agent.session=store.get(agent.session.id)
    try:
        events=await run(agent,'Спланируй упаковку проекта')
        result=json.loads(next(e for e in events if e['type']=='tool_result')['content'])
        assert result['available'] is False
        prompt=seen[0]['messages'][0]['content']
        assert 'Не оставляй выполнимые сейчас проверки' in prompt
        assert 'explorer' in prompt and 'reviewer' in prompt
        assert 'pyproject.toml отсутствует' in next(e for e in events if e['type']=='assistant' and e.get('content'))['content']
    finally:
        await agent.llm.http.aclose();store.close()


async def test_inspect_http_real_local_server_and_ssrf(tmp_path):
    from fastapi import FastAPI
    from fastapi.responses import Response, RedirectResponse
    from tests.fakes import ServerThread
    from agent.settings import merge
    app=FastAPI()
    @app.head('/ready')
    async def ready():
        return Response(status_code=204)
    @app.get('/only-get')
    async def only_get():
        return Response('ok')
    @app.head('/redirect')
    async def redirect():
        return RedirectResponse('/ready')
    tool=Inspect()
    with ServerThread(app) as server:
        # ServerThread использует обычный HTTP для локальных инструментов.
        ctx=ToolContext(tmp_path,merge(DEFAULTS,{'network':{'allow_private':True}}))
        url=f'http://127.0.0.1:{server.port}'
        result=json.loads(await tool.run({'kind':'http','target':url+'/redirect'},ctx))
        assert result['status']==204 and result['available'] is True
        result=json.loads(await tool.run({'kind':'http','target':url+'/only-get'},ctx))
        assert result['status']==405 and result['available'] is None and not result['head_supported']
        ctx.settings=DEFAULTS
        with pytest.raises(ToolError,match='внутренние адреса'):
            await tool.run({'kind':'http','target':url+'/ready'},ctx)


async def test_read_only_defaults_to_explorer_and_keeps_protected_files(tmp_path, monkeypatch):
    seen=[]
    secret=tmp_path/'protected.pem';secret.write_text('защищённое содержимое')
    agent,store,_=make_agent(tmp_path,[
        {'tool_calls':[tool_call('delegate','subagent',task='Прочитай конфигурацию')]},
        {'tool_calls':[tool_call('read','read_file',path='protected.pem')]},
        {'content':'Путь защищён, нужна другая проверка'},
        {'content':'План с учётом недоступных данных'},
    ],seen,permissions={'mode':'read_only'})
    agent.registry.add(Subagent());agent.registry.add(ReadFile())
    from agent import credentials
    monkeypatch.setattr(credentials, '_PROTECTED', {secret.resolve()})
    try:
        events=await run(agent)
        start=next(e for e in events if e['type']=='subagent_start')
        assert start['role']=='explorer'
        result=next(e['event'] for e in events if e['type']=='subagent_event' and e['event']['type']=='tool_result')
        assert not result['ok'] and 'защищённое содержимое' not in result['content']
        assert events[-1]['reason']=='stop'
    finally:
        await agent.llm.http.aclose();store.close()


def test_inspection_and_role_argument_bounds():
    for args in ({'task':'x'*16001}, {'task':'task','context':'x'*16001}, {'task':' '}):
        with pytest.raises(ToolError):
            Subagent().check_args(args)
    for target in (' ', 'x'*4001):
        with pytest.raises(ToolError):
            Inspect().check_args({'kind':'path','target':target})


async def test_plan_handoff_and_clarifications_are_explicit(tmp_path):
    agent,store,_=make_agent(tmp_path,[])
    try:
        store.update(agent.session.id,agent_mode='plan');agent.session=store.get(agent.session.id)
        prompt=agent.system_prompt()
        assert 'предложи перейти в Build' in prompt and '/mode build' in prompt
        assert 'задай question' in prompt and 'Не спрашивай то, что можешь проверить' in prompt
        assert 'Не переключай режим' in prompt
        store.update(agent.session.id,agent_mode='build');agent.session=store.get(agent.session.id)
        assert 'используй question' in agent.system_prompt()
    finally:
        await agent.llm.http.aclose();store.close()
