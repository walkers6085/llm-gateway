"""Регрессии аудита: доверие, секреты, потоки, отмена, контекст, файловые операции."""

import asyncio
import copy
import io
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import pytest

from agent.cli import amain, safe_terminal
from agent.credentials import Credentials, register
from agent.hooks import Hooks
from agent.llm import LLMClient, LLMError
from agent.providers import ProviderManager
from agent.settings import DEFAULTS, SettingsError, load, merge
from agent.store import SessionStore
from agent.tools.base import ToolContext, ToolError
from agent.tools.files import EditFile, Grep, ReadFile, WriteFile
from agent.tools import web
from agent.trust import TrustStore
from bench.client import BenchClient, Completion
from bench.runner import Result, compare, make_report
from gateway.tokens import TokenStore
from tests.test_loop import make_agent, run
from tests.fakes import tool_call


def test_project_trust_is_content_bound(tmp_path):
    path = tmp_path / '.gwagent/settings.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'hooks': {'SessionStart': [{'command': 'false'}]}, 'gateway': {'url': 'http://untrusted.invalid'}}))
    with pytest.raises(SettingsError, match='не доверен'):
        load(tmp_path)
    TrustStore().set(tmp_path)
    assert load(tmp_path)['gateway']['url'] == 'http://untrusted.invalid'
    path.write_text(json.dumps({'model': 'changed'}))
    with pytest.raises(SettingsError, match='изменён'):
        load(tmp_path)
    TrustStore().set(tmp_path)
    TrustStore().set(tmp_path, False)
    with pytest.raises(SettingsError):
        load(tmp_path)


async def test_trust_cli(tmp_path):
    path = tmp_path / '.gwagent/settings.json'
    path.parent.mkdir(); path.write_text('{}')
    out = io.StringIO()
    assert await amain(['trust','--cwd',str(tmp_path)], read=lambda _: 'n', out=out) == 1
    assert not TrustStore().trusted(tmp_path)
    assert await amain(['trust','--cwd',str(tmp_path)], read=lambda _: 'y', out=out) == 0
    assert TrustStore().trusted(tmp_path)


async def test_cross_origin_redirect_never_sends_header(tmp_path):
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(302, headers={'location': 'https://other.invalid/secret'})
    ctx = ToolContext(tmp_path, merge(DEFAULTS, {'network': {'allow_private': True}}))
    with patch.object(web, 'http_client', lambda _: httpx.AsyncClient(transport=httpx.MockTransport(handler))):
        with pytest.raises(ToolError, match='другой origin'):
            await web.request(ctx,'GET','https://first.invalid',headers={'Authorization':'Bearer dummy'})
    assert len(seen) == 1


@pytest.mark.parametrize('body', [
    'data: {"choices":[{"delta":{"content":"часть"}}]}\n\n',
    'data: not-json\n\n',
    'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\ndata: [DONE]\n\n',
    'data: {"choices":[{"delta":{},"finish_reason":"content_filter"}]}\n\n'])
async def test_llm_incomplete_or_malformed_stream(body):
    async with httpx.AsyncClient(base_url='http://up/v1',transport=httpx.MockTransport(lambda _:httpx.Response(200,text=body,headers={'content-type':'text/event-stream'}))) as http:
        with pytest.raises(LLMError):
            _ = [e async for e in LLMClient(http).stream('model',[])]


async def test_benchmark_unknown_usage_and_incomplete_stream():
    client = BenchClient('http://test/v1',transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'choices':[{'message':{'content':'ok'}}]})))
    r = await client.complete('model',[])
    assert not r.usage_reported and r.decode_tps is None
    await client.close()
    client = BenchClient('http://test/v1',transport=httpx.MockTransport(lambda _:httpx.Response(200,text='data: {"choices":[]}\n\n',headers={'content-type':'text/event-stream'})))
    assert not (await client.complete('model',[])).ok
    await client.close()
    assert Completion(status=200, completion_tokens=20, ttft=1,total=2).decode_tps is None


async def test_cancel_at_assistant_and_crash_recovery(tmp_path):
    agent, store, _ = make_agent(tmp_path, [{'tool_calls':[tool_call('c1','echo',text='x')]}])
    stream = agent.run('x')
    while (await anext(stream))['type'] != 'assistant': pass
    await stream.aclose()
    assert [m.get('tool_call_id') for m in store.messages(agent.session.id) if m['role']=='tool'] == ['c1']
    # Имитируем сохранение assistant непосредственно перед аварийным завершением процесса.
    store.add_message(agent.session.id, {'role':'assistant','content':None,'tool_calls':[{'id':'c2'}]})
    store.repair_calls(agent.session.id)
    store.repair_calls(agent.session.id)
    assert sum(m.get('tool_call_id')=='c2' for m in store.messages(agent.session.id)) == 1
    assert store.db.execute('SELECT state FROM agent_turns').fetchone()[0] == 'interrupted'
    await agent.llm.http.aclose()
    store.close()


async def test_cancel_hook_kills_actual_process(tmp_path):
    original = asyncio.create_subprocess_exec
    children = []
    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        children.append(process)
        return process
    hooks = Hooks({'SessionStart':[{'command':'sleep 60'}]},cwd=str(tmp_path))
    with patch('agent.hooks.asyncio.create_subprocess_exec', capture):
        task = asyncio.create_task(hooks.run('SessionStart',{}))
        for _ in range(100):
            if children: break
            await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    assert children and children[0].returncode is not None


async def test_budget_prevents_request_and_compact_preserves_history(tmp_path):
    agent, store, usage = make_agent(tmp_path,[{'content':'unused'}],max_input_tokens=1)
    events = await run(agent)
    assert events[-1]['reason']=='budget' and not usage
    sid = agent.session.id
    for i in range(4):
        store.add_message(sid,{'role':'user','content':str(i)})
        store.add_message(sid,{'role':'assistant','content':str(i)})
    before = store.messages(sid)
    assert store.compact(sid) and store.messages(sid) == before
    assert store.context_messages(sid)[0]['role']=='user'
    assert store.get(sid).summary
    await agent.llm.http.aclose(); store.close()


def test_credentials_permissions_redaction_and_concurrent_provider_write(tmp_path):
    path=tmp_path/'providers.json'
    manager=ProviderManager(path)
    manager.add('local','http://local/v1',model='m')
    assert not manager.selected
    manager.connect('local',read=lambda _:'bearer',password=lambda _:'audit-secret-123')
    assert manager.connection('local').token=='audit-secret-123'
    assert 'audit-secret' not in path.read_text() and 'audit-secret' not in repr(manager.connection('local'))
    auth=path.with_name('auth.json')
    assert stat.S_IMODE(auth.stat().st_mode)==0o600
    other=ProviderManager(path)
    manager.use('local')
    with pytest.raises(ValueError,match='другим процессом'): other.update('local',model='changed')
    assert other.get('local')['model']=='m'
    assert ProviderManager(path).get('local')['model']=='m'
    auth.chmod(0o644)
    with pytest.raises(ValueError,match='0600'): Credentials(auth).read()


async def test_provider_mtls_real_handshake(tmp_path,pki,upstream_url):
    manager=ProviderManager(tmp_path/'providers.json')
    manager.add('mtls',upstream_url,model='alpha')
    manager.update('mtls',tls={'cert':pki['client_cert'],'key':pki['client_key'],'ca':pki['ca']})
    assert 'alpha' in await manager.models('mtls')
    connection=manager.connection('mtls')
    client=BenchClient(connection.url,connection=connection)
    assert (await client.complete('alpha',[{'role':'user','content':'привет'}])).ok
    await client.close()


async def test_file_conflict_preview_modes_and_secret_protection(tmp_path):
    ctx=ToolContext(tmp_path,copy.deepcopy(DEFAULTS))
    path=tmp_path/'a.txt';path.write_text('old\n');path.chmod(0o640)
    await ReadFile().run({'path':'a.txt'},ctx)
    path.write_text('external\n')
    with pytest.raises(ToolError,match='изменён'):
        await WriteFile().run({'path':'a.txt','content':'new\n'},ctx)
    await ReadFile().run({'path':'a.txt'},ctx)
    preview=await EditFile().run({'path':'a.txt','old_string':'external','new_string':'new','preview':True},ctx)
    assert '-external' in preview and path.read_text()=='external\n'
    await EditFile().run({'path':'a.txt','old_string':'external','new_string':'new'},ctx)
    assert stat.S_IMODE(path.stat().st_mode)==0o640
    Credentials(tmp_path/'auth.json').set('test',{'token':'secret'})
    with pytest.raises(ToolError,match='учётных'):
        await ReadFile().run({'path':'auth.json'},ctx)


async def test_regex_timeout_is_interruptible(tmp_path):
    (tmp_path/'evil.txt').write_text('a'*100000+'!')
    ctx=ToolContext(tmp_path,merge(DEFAULTS,{'files':{'timeout':1}}))
    ticks=[]
    async def heartbeat():
        for i in range(10):
            ticks.append(i);await asyncio.sleep(.1)
    with pytest.raises(ToolError,match='таймаут'):
        await asyncio.gather(Grep().run({'pattern':'(a+)+$'},ctx),heartbeat())
    assert len(ticks)>=5


def test_sqlite_concurrent_operations_and_private_file(tmp_path):
    store=TokenStore(str(tmp_path/'tokens.db'))
    def work(index):
        raw, token=store.create(str(index))
        for _ in range(50):
            assert store.lookup(raw).id==token.id
            store.record(token.id,'test','model',200)
    with ThreadPoolExecutor(max_workers=24) as pool: list(pool.map(work,range(30)))
    assert len(store.all())==30 and stat.S_IMODE((tmp_path/'tokens.db').stat().st_mode)==0o600
    store.close()
    sessions=SessionStore(tmp_path/'sessions.db');session=sessions.create('local')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n:sessions.add_message(session.id,{'role':'user','content':str(n)}),range(100)))
    assert len(sessions.messages(session.id))==100
    sessions.close()


def test_report_completeness_and_version_warning():
    bad=Result('tools','Инструменты',quality=True,score=0,infrastructure_errors=1)
    report=make_report('model','http://test',{},[bad],{})
    assert not report['complete'] and report['overall'] is None
    other={**report,'method_version':'different'}
    assert 'несопоставимы' in compare([report,other])


def test_terminal_controls_removed():
    assert safe_terminal('\x1b[2Jпривет\x1b]52;c;bad\x07\r')=='привет'


async def test_registered_secrets_do_not_enter_history(tmp_path):
    agent,store,_=make_agent(tmp_path,[{'content':'safe'}])
    register('audit-secret-long')
    await run(agent,'audit-secret-long')
    assert 'audit-secret-long' not in json.dumps(store.messages(agent.session.id))
    await agent.llm.http.aclose();store.close()


async def test_model_capabilities_control_request():
    seen=[]
    def handler(request):
        body=json.loads(request.content);seen.append(body)
        return httpx.Response(200,json={'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]})
    async with httpx.AsyncClient(base_url='http://up/v1',transport=httpx.MockTransport(handler)) as http:
        llm=LLMClient(http,options={'stream':False,'temperature':False,'usage':False,'max_tokens_field':'max_completion_tokens'})
        events=[e async for e in llm.stream('model',[],temperature=.7)]
        assert events[-1]['message']['content']=='ok'
        assert 'temperature' not in seen[0] and 'stream_options' not in seen[0]
        assert 'max_completion_tokens' in seen[0] and seen[0]['stream'] is False
        with pytest.raises(LLMError,match='не поддерживает'):
            llm.options['tools']=False
            _=[e async for e in llm.stream('model',[],tools=[{'type':'function'}])]


async def test_doctor_checks_tls_api_and_database_without_creating_it(tmp_path):
    from agent.doctor import diagnose
    manager=ProviderManager(tmp_path/'providers.json')
    manager.add('local','http://local/v1',model='model')
    settings=merge(DEFAULTS,{'sessions':{'db':str(tmp_path/'absent.db')}})
    checks=await diagnose(settings,'local',str(manager.path),transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'data':[{'id':'model'}]})))
    assert all(c['ok'] for c in checks), checks
    assert not (tmp_path/'absent.db').exists()


def test_config_show_explains_sources_without_tokens(tmp_path):
    from agent import settings
    settings.USER_SETTINGS.parent.mkdir(parents=True,exist_ok=True)
    settings.USER_SETTINGS.write_text(json.dumps({'gateway':{'token':'not-for-display'},'model':'mine'}))
    result=settings.explain(tmp_path)
    assert result['sources']['model']==str(settings.USER_SETTINGS)
    assert 'not-for-display' not in json.dumps(result)



def test_stored_credentials_bound_to_endpoint(tmp_path):
    manager=ProviderManager(tmp_path/'providers.json')
    manager.add('local','https://first.invalid/v1',model='m')
    manager.connect('local',read=lambda _:'bearer',password=lambda _:'dummy-long-key')
    manager.update('local',url='https://other.invalid/v1')
    with pytest.raises(ValueError,match='назначение'):
        manager.connection('local')


async def test_basic_auth_uses_header_and_stays_out_of_config(tmp_path):
    manager=ProviderManager(tmp_path/'providers.json')
    manager.add('basic','https://example.invalid/v1',model='m')
    answers=iter(['basic','user'])
    manager.connect('basic',read=lambda _:next(answers),password=lambda _:'audit-password-123')
    def handler(req):
        assert req.headers['authorization'].startswith('Basic ')
        assert 'password' not in str(req.url)
        return httpx.Response(200,json={'data':[{'id':'m'}]})
    assert await manager.models('basic',transport=httpx.MockTransport(handler))==['m']
    assert 'audit-password' not in manager.path.read_text()


async def test_export_and_delete_cli_commands(tmp_path):
    from agent.cli import App, Console, parse_args
    config=merge(DEFAULTS,{'sessions':{'db':str(tmp_path/'history.db')}})
    app=App(parse_args([]),config,tmp_path,Console(io.StringIO()),read=lambda _:'y')
    app.session=app.store.create('local',model='m',workspace=str(tmp_path))
    app.store.add_message(app.session.id,{'role':'user','content':'history'})
    old=app.store.create('local',model='m',workspace=str(tmp_path))
    path=tmp_path/'export.json'
    await app.command('/export '+str(path))
    assert json.loads(path.read_text())['messages'][0]['content']=='history'
    await app.command('/delete '+old.id)
    assert app.store.get(old.id) is None
    await app.close()


async def test_provider_encrypted_private_key(tmp_path,pki,upstream_url):
    from pathlib import Path
    from cryptography.hazmat.primitives import serialization
    private=serialization.load_pem_private_key(Path(pki['client_key']).read_bytes(),password=None)
    encrypted=tmp_path/'encrypted.key'
    encrypted.write_bytes(private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
                                               serialization.BestAvailableEncryption(b'audit-key-password')))
    manager=ProviderManager(tmp_path/'providers.json')
    manager.add('mtls',upstream_url,model='alpha')
    manager.update('mtls',tls={'cert':pki['client_cert'],'key':str(encrypted),'ca':pki['ca']})
    manager.connect('mtls',read=lambda _:'none',password=lambda _:'audit-key-password')
    assert 'alpha' in await manager.models('mtls')
    assert 'audit-key-password' not in manager.path.read_text()


async def test_config_cli_shows_selected_connection(tmp_path):
    manager=ProviderManager()
    manager.add('selected','http://selected.invalid/v1',model='specific')
    manager.use('selected')
    output=io.StringIO()
    assert await amain(['config','show','--cwd',str(tmp_path)],out=output)==0
    data=json.loads(output.getvalue())
    assert data['connection']['url']=='http://selected.invalid/v1'
    assert data['values']['model']=='specific'


async def test_trust_rejects_change_during_confirmation(tmp_path):
    path=tmp_path/'.gwagent/settings.json';path.parent.mkdir();path.write_text('{}')
    def changed(_):
        path.write_text('{"model":"changed"}')
        return 'y'
    out,err=io.StringIO(),io.StringIO()
    assert await amain(['trust','--cwd',str(tmp_path)],read=changed,out=out,err=err)==2
    assert 'изменился' in err.getvalue() and not TrustStore().trusted(tmp_path)
