"""JSONL-мост проверяется отдельным процессом против настоящего локального шлюза."""
import asyncio
import io
import json
import os
import sys
from pathlib import Path

import pytest
from agent import tui
from agent.tui_backend import Bridge
from tests.test_cli import gw  # общая mTLS-фикстура CLI
from tests.fakes import SCRIPT, SEEN, tool_call


async def launch(gw, tmp_path, env_overrides=None):
    cfg, ws, _ = gw
    env = dict(os.environ, HOME=str(tmp_path / 'home'), XDG_CONFIG_HOME=str(tmp_path / 'config'))
    env['GWAGENT_HOME'] = str(tmp_path / 'agent-home')
    env.update(env_overrides or {})
    return await asyncio.create_subprocess_exec(sys.executable, '-m', 'agent.tui_backend', '--settings', str(cfg),
        '--cwd', str(ws), env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)


async def receive(proc, kind):
    events = []
    async with asyncio.timeout(10):
        while True:
            line = await proc.stdout.readline()
            assert line, await proc.stderr.read()
            event = json.loads(line)
            events.append(event)
            if event['type'] == kind:
                return event, events


def send(proc, **message):
    proc.stdin.write((json.dumps(message)+'\n').encode())


async def stop(proc):
    if proc.returncode is None:
        send(proc, type='shutdown')
        await asyncio.wait_for(proc.wait(), 5)
    assert proc.returncode == 0, await proc.stderr.read()


async def test_bridge_session_tables_permissions_cancel_and_shutdown(gw, tmp_path):
    _, ws, db = gw
    SCRIPT.extend([{'content':'Важно: проверка', 'tool_calls':[tool_call('write', 'write_file', path='x', content='ok')]},
                   {'content':'Готово'}, {'tool_calls':[tool_call('cancel', 'shell', command='touch cancelled')]},
                   {'content':'Снова работаю'}])
    proc = await launch(gw, tmp_path)
    try:
        _, events = await receive(proc, 'ready')
        session = next(e['session']['id'] for e in events if e['type']=='snapshot')
        for command in ('/sessions','/tools','/models','/provider','/mcp'):
            send(proc, type='submit', text=command)
            _, events = await receive(proc, 'idle')
            table = next(e for e in events if e['type']=='table')
            assert table['columns'] and isinstance(table['rows'],list)
        send(proc, type='submit', text='Запиши x')
        request, _ = await receive(proc, 'permission_request')
        assert request['id']=='write' and not (ws/'x').exists()
        send(proc, type='approve', id='write', decision='allow')
        _, events = await receive(proc, 'idle')
        assert (ws/'x').read_text()=='ok'
        assert next(e for e in events if e['type']=='snapshot')['session']['id']==session
        assert any(e['type']=='done' and e['reason']=='stop' for e in events)
        send(proc, type='submit', text='Отмена')
        await receive(proc,'permission_request')
        send(proc,type='cancel')
        _, events = await receive(proc,'idle')
        assert not (ws/'cancelled').exists()
        assert any(e['type']=='done' and e['reason']=='cancelled' for e in events)
        # После отмены история остаётся пригодной для следующего хода.
        send(proc,type='submit',text='Продолжай')
        _, events = await receive(proc,'idle')
        assert any(e['type']=='text' and 'Снова' in e['text'] for e in events)
        proc.stdin.write(b'not json\n')
        event,_=await receive(proc,'notice')
        assert event['level']=='error'
    finally:
        await stop(proc)


async def test_bridge_connect_dialog_cancellation_does_not_hang(gw,tmp_path):
    proc=await launch(gw,tmp_path)
    try:
        await receive(proc,'ready')
        send(proc,type='submit',text='/connect')
        event,_=await receive(proc,'input_request')
        assert event['prompt']
        assert 'URL API' in event['prompt']
        send(proc,type='answer',id=event['id'],value='')
        event,_=await receive(proc,'input_request')
        assert [choice['value'] for choice in event.get('choices',[])]==['bearer','mtls','basic','none']
        send(proc,type='cancel')
        await receive(proc,'idle')
        send(proc,type='submit',text='/tools')
        event,_=await receive(proc,'table')
        assert event['rows']
        await receive(proc,'idle')
    finally:
        await stop(proc)


async def test_bridge_does_not_approve_unknown_requests():
    bridge=Bridge([],output=io.StringIO())
    with pytest.raises(ValueError):
        bridge.handle({'type':'approve','id':'unknown','decision':'allow'})
    task=asyncio.create_task(bridge.approve({'type':'permission_request','id':'one'}))
    await asyncio.sleep(0)
    with pytest.raises(ValueError):
        bridge.handle({'type':'approve','id':'one','decision':'yes'})
    assert not task.done()
    bridge.handle({'type':'approve','id':'one','decision':'deny'})
    assert await task=='deny'
    assert not bridge.pending


def test_launcher_fallback_and_explicit_errors(monkeypatch,capsys):
    monkeypatch.setattr(sys.stdin,'isatty',lambda:False)
    monkeypatch.setattr(sys.stdout,'isatty',lambda:False)
    assert tui.dispatch([]) is None
    assert tui.dispatch(['--plain']) is None
    assert tui.dispatch(['-p','test','--json']) is None
    assert tui.dispatch(['--tui'])==2
    assert tui.dispatch(['--tui','-p','test'])==2
    assert '--tui' in capsys.readouterr().err


def test_launcher_install_and_forwarding(monkeypatch,tmp_path):
    source=tui.assets()
    monkeypatch.setenv('XDG_CACHE_HOME',str(tmp_path))
    monkeypatch.setattr(tui,'bun_binary',lambda:'/mock/bun')
    calls=[]
    def run(argv,**kwargs):
        calls.append((argv,kwargs))
        return type('Result',(),{'returncode':0})()
    monkeypatch.setattr(tui.subprocess,'run',run)
    assert tui.install()==0
    assert calls[-1][0][-2:]==['--frozen-lockfile','--ignore-scripts']
    assert (tui.cache_path(source)/'src/main.ts').is_file()
    monkeypatch.setattr(sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(sys.stdout,'isatty',lambda:True)
    monkeypatch.setattr(tui,'installed',lambda _:source)
    assert tui.dispatch(['--tui','--cwd',str(tmp_path)])==0
    argv,kwargs=calls[-1]
    assert argv[-2:]==['--cwd',str(tmp_path)] and '--tui' not in argv
    assert kwargs['env']['GWAGENT_PYTHON']==sys.executable


async def test_bridge_connect_stores_secret_without_echo(gw,tmp_path):
    proc=await launch(gw,tmp_path)
    secret='test-secret-never-echo-9812'
    try:
        await receive(proc,'ready')
        send(proc,type='submit',text='/connect')
        event,_=await receive(proc,'input_request')
        assert not event['secret'] and 'URL API' in event['prompt']
        send(proc,type='answer',id=event['id'],value='')
        event,_=await receive(proc,'input_request')
        send(proc,type='answer',id=event['id'],value='bearer')
        event,_=await receive(proc,'input_request')
        assert event['secret']
        send(proc,type='answer',id=event['id'],value=secret)
        _,events=await receive(proc,'idle')
        assert secret not in json.dumps(events)
        auth=tmp_path/'agent-home/auth.json'
        assert secret in auth.read_text()
        assert auth.stat().st_mode & 0o777 == 0o600
    finally:
        await stop(proc)


async def test_offline_start_commands_and_provider_setup_then_chat(gw,tmp_path):
    cfg,ws,_=gw
    proc=await launch(gw,tmp_path,{'GW_TOKEN':''})
    secret=os.environ['GW_TOKEN']
    url=json.loads(cfg.read_text())['gateway']['url']
    SCRIPT.append({'content':'Ответ после настройки'})
    try:
        _,events=await receive(proc,'ready')
        assert not SEEN  # открытие UI не обращается даже к /models
        assert not any(e['type']=='fatal' for e in events)
        for command in ('/help','/provider','/tools','/sessions','/new','/model custom'):
            send(proc,type='submit',text=command)
            _,events=await receive(proc,'idle')
            assert not any(e['type']=='error' for e in events)
        assert not SEEN
        send(proc,type='submit',text='Сообщение без ключа')
        _,events=await receive(proc,'idle')
        assert any(e['type']=='user' for e in events)
        assert any(e['type']=='error' and '/connect' in e['message'] for e in events)
        assert not any(e['type']=='fatal' for e in events)
        send(proc,type='submit',text='/models')
        _,events=await receive(proc,'idle')
        assert any(e['type']=='warning' for e in events)
        assert any(e['type']=='table' for e in events)
        # Новый провайдер настраивается внутри того же UI, даже без модели/ключа шлюза.
        send(proc,type='submit',text='/connect local')
        for value in (url,'bearer',secret):
            request,_=await receive(proc,'input_request')
            assert request['secret']==(value==secret)
            send(proc,type='answer',id=request['id'],value=value)
        _,events=await receive(proc,'idle')
        assert not SEEN
        assert secret not in json.dumps(events)
        snapshot=next(e for e in events if e['type']=='snapshot')
        assert snapshot['provider']=='local' and snapshot['session']['model']==''
        send(proc,type='submit',text='/models')
        _,events=await receive(proc,'idle')
        assert any(row[1]=='alpha' for row in next(e['rows'] for e in events if e['type']=='table'))
        send(proc,type='submit',text='Теперь ответь')
        _,events=await receive(proc,'idle')
        assert 'Ответ после настройки' in ''.join(e['text'] for e in events if e['type']=='text'), events
        assert next(e['session']['model'] for e in events if e['type']=='snapshot')=='alpha'
    finally:
        await stop(proc)


async def test_unreachable_api_keeps_commands_and_chat_available(gw,tmp_path):
    cfg,_,_=gw
    config=json.loads(cfg.read_text())
    config['gateway']['url']='http://127.0.0.1:1/v1'
    cfg.write_text(json.dumps(config))
    proc=await launch(gw,tmp_path)
    try:
        await receive(proc,'ready')
        assert not SEEN
        for command in ('/models','попробуй ответить'):
            send(proc,type='submit',text=command)
            _,events=await receive(proc,'idle')
            assert any(e['type']==('warning' if command=='/models' else 'error') for e in events)
            assert not any(e['type']=='fatal' for e in events)
            assert proc.returncode is None
        send(proc,type='submit',text='/tools')
        _,events=await receive(proc,'idle')
        assert any(e['type']=='table' and e['rows'] for e in events)
    finally:
        await stop(proc)


async def test_slash_catalog_matches_enabled_builtin_and_mcp_without_model(gw,tmp_path):
    from tests.test_cli import MCP
    cfg,ws,_=gw
    config=json.loads(cfg.read_text())
    config['tools']={'enabled':['read_file']}
    config['mcp']={'servers':{'test':{'type':'stdio','command':sys.executable,'args':[MCP]}}}
    cfg.write_text(json.dumps(config))
    proc=await launch(gw,tmp_path,{'GW_TOKEN':''})
    try:
        _,events=await receive(proc,'ready')
        snapshot=next(e for e in events if e['type']=='snapshot')
        catalog=snapshot['catalog']
        commands=[e['command'] for e in catalog if e['kind']=='command']
        names=[e['name'] for e in catalog if e['kind']=='tool']
        assert '/connect' in commands and '/provider add' in commands and '/tools' in commands
        assert 'read_file' in names and 'shell' not in names and 'write_file' not in names
        assert 'mcp__test__echo' in names
        assert not SEEN
        for name in ('read_file','mcp__test__echo'):
            send(proc,type='submit',text='/tools '+name)
            _,events=await receive(proc,'idle')
            assert any(e['type']=='table' and e['rows'] for e in events)
            assert any(e['type']=='notice' and name in e['message'] for e in events)
            assert not any(e['type'] in ('tool_call','permission_request','user') for e in events)
        send(proc,type='submit',text='/tools shell')
        _,events=await receive(proc,'idle')
        assert any(e['type']=='error' for e in events)
        assert not SEEN
        assert not list(ws.iterdir())
    finally:
        await stop(proc)


async def test_model_selection_metadata_persistence_and_next_request(gw,tmp_path):
    from agent.store import SessionStore
    _,_,db=gw
    proc=await launch(gw,tmp_path)
    SCRIPT.append({'content':'Выбранная модель работает'})
    try:
        _,events=await receive(proc,'ready')
        session=next(e['session']['id'] for e in events if e['type']=='snapshot')
        send(proc,type='submit',text='/model beta')
        await receive(proc,'idle')
        send(proc,type='submit',text='/models')
        _,events=await receive(proc,'idle')
        table=next(e for e in events if e['type']=='table')
        assert table['selection']['current']=='beta' and table['selection']['session_id']==session
        assert table['selection']['provider'] is None
        assert {'alpha','beta'} <= {row[1] for row in table['rows']}
        send(proc,type='select',kind='model',provider=None,model='alpha',session_id=session)
        _,events=await receive(proc,'idle')
        snapshot=next(e for e in events if e['type']=='snapshot')
        assert snapshot['session']['id']==session and snapshot['session']['model']=='alpha'
        store=SessionStore(db)
        try:
            assert store.get(session).model=='alpha'
        finally:
            store.close()
        send(proc,type='submit',text='Проверка выбранной модели')
        _,events=await receive(proc,'idle')
        assert any(e['type']=='done' and e['reason']=='stop' for e in events)
        assert SEEN[-1]['body']['model']=='alpha'
    finally:
        await stop(proc)


async def test_models_from_all_providers_switch_together_and_preserve_old_history(gw,tmp_path):
    from agent.store import SessionStore
    cfg,_,db=gw
    url=json.loads(cfg.read_text())['gateway']['url']
    home=tmp_path/'agent-home';home.mkdir(parents=True)
    providers={name:{'url':url,'token_env':'GW_TOKEN','model':model,'models':[]}
               for name,model in [('first','alpha'),('second','beta')]}
    providers['broken']={'url':url,'token_env':'UNSET_CATALOG_KEY_9356','model':'cached','models':['cached']}
    providers['first']['tags']={'tagged-free':['free']}
    providers['disabled']={**providers['first'],'enabled':False}
    (home/'providers.json').write_text(json.dumps({'default':'first','selected':True,'providers':providers}))
    proc=await launch(gw,tmp_path,{'UNSET_CATALOG_KEY_9356':''})
    SCRIPT.extend([{'content':'Первая история'},{'content':'Вторая история'}])
    try:
        _,events=await receive(proc,'ready')
        first=next(e['session']['id'] for e in events if e['type']=='snapshot')
        send(proc,type='submit',text='Первый запрос')
        await receive(proc,'idle')
        send(proc,type='submit',text='/models')
        _,events=await receive(proc,'idle')
        table=next(e for e in events if e['type']=='table')
        choices=table['selection']['choices']
        assert {('first','alpha'),('first','beta'),('second','alpha'),('second','beta'),('broken','cached'),
                ('first','tagged-free')} <= {(c['provider'],c['model']) for c in choices}
        tagged=next(c for c in choices if (c['provider'],c['model'])==('first','tagged-free'))
        assert tagged['available'] is True and tagged['tags']==['free']
        assert not any(c['provider']=='disabled' for c in choices)
        assert next(c for c in choices if c['provider']=='broken')['available'] is False
        assert any(e['type']=='warning' and 'broken' in e['message'] for e in events)
        for provider,model in [('first','not-offered'),('disabled','alpha')]:
            send(proc,type='select',kind='model',provider=provider,model=model,session_id=first)
            _,rejected=await receive(proc,'idle')
            assert any(e['type']=='error' for e in rejected)
        send(proc,type='select',kind='model',provider='second',model='beta',session_id=first)
        _,events=await receive(proc,'idle')
        snapshot=next(e for e in events if e['type']=='snapshot')
        second=snapshot['session']['id']
        assert second!=first and snapshot['provider']=='second' and snapshot['session']['model']=='beta'
        send(proc,type='submit',text='Второй запрос')
        await receive(proc,'idle')
        assert SEEN[-1]['body']['model']=='beta'
        assert [m['content'] for m in SEEN[-1]['body']['messages'] if m['role']=='user']==['Второй запрос']
        store=SessionStore(db)
        try:
            assert store.get(first).model=='alpha' and store.get(first).profile=='provider:first'
            assert any(m.get('content')=='Первый запрос' for m in store.messages(first))
        finally:store.close()
        # Повторный выбор из старой сессии отклоняется и не блокирует ввод.
        send(proc,type='select',kind='model',provider='first',model='alpha',session_id=first)
        _,events=await receive(proc,'idle')
        assert any(e['type']=='error' and 'устарел' in e['message'] for e in events)
        send(proc,type='submit',text='/provider')
        _,events=await receive(proc,'idle')
        selection=next(e['selection'] for e in events if e['type']=='table')
        assert selection['kind']=='provider' and selection['provider']=='second'
        assert next(c for c in selection['choices'] if c['provider']=='disabled')['enabled'] is False
        send(proc,type='select',kind='provider',provider='first',session_id=second)
        _,events=await receive(proc,'idle')
        assert next(e['provider'] for e in events if e['type']=='snapshot')=='first'
    finally:await stop(proc)


async def test_sessions_picker_resume_workspace_guard_and_confirmed_delete(gw,tmp_path):
    from agent.store import SessionStore
    _,ws,db=gw
    proc=await launch(gw,tmp_path,{'GW_TOKEN':''})
    try:
        _,events=await receive(proc,'ready')
        initial=next(e['session']['id'] for e in events if e['type']=='snapshot')
        store=SessionStore(db)
        old=store.create('local',model='alpha',workspace=str(ws))
        store.update(old.id,title='Рабочая сессия')
        store.add_message(old.id,{'role':'user','content':'Сохранённая история'})
        other=store.create('local',model='alpha',workspace=str(tmp_path/'other'))
        private=store.create('another-owner',model='alpha',workspace=str(ws))
        victim=store.create('local',model='alpha',workspace=str(ws))
        outdated=store.create('local',model='alpha',workspace=str(ws),profile='provider:glm',endpoint_hash='old-endpoint')
        send(proc,type='submit',text='/sessions')
        _,events=await receive(proc,'idle')
        selection=next(e['selection'] for e in events if e['type']=='table')
        assert selection['kind']=='session' and selection['current']==initial
        by_id={c['id']:c for c in selection['choices']}
        assert by_id[old.id]['enabled'] and not by_id[other.id]['enabled'] and private.id not in by_id
        send(proc,type='select',kind='session',target=other.id,session_id=initial)
        _,events=await receive(proc,'idle')
        assert any(e['type']=='error' for e in events)
        send(proc,type='select',kind='session',target=outdated.id,session_id=initial)
        _,rejected=await receive(proc,'idle')
        assert any(e['type']=='error' for e in rejected)
        send(proc,type='submit',text='/help')
        _,unchanged=await receive(proc,'idle')
        assert next(e['provider'] for e in unchanged if e['type']=='snapshot')=='gateway'
        send(proc,type='select',kind='session',target=old.id,session_id=initial)
        _,events=await receive(proc,'idle')
        snapshot=next(e for e in events if e['type']=='snapshot')
        assert snapshot['session']['id']==old.id
        assert snapshot['history']==[{'role':'user','content':'Сохранённая история'}]
        for answer in ('n','y'):
            send(proc,type='submit',text='/delete')
            _,events=await receive(proc,'idle')
            choices=next(e['selection']['choices'] for e in events if e['type']=='table')
            assert next(c for c in choices if c['id']==old.id)['enabled'] is False
            send(proc,type='select',kind='session-delete',target=victim.id,session_id=old.id)
            request,_=await receive(proc,'input_request')
            assert request['choices'][0]['value']=='n'  # безопасный выбор по умолчанию
            send(proc,type='answer',id=request['id'],value=answer)
            await receive(proc,'idle')
            assert (store.get(victim.id) is None)==(answer=='y')
        assert store.get(old.id) and store.get(initial)
        assert not SEEN
        store.close()
    finally:await stop(proc)


@pytest.mark.parametrize('mode',['message','files','cancel'])
async def test_revert_picker_edit_and_file_rollback_without_model_call(gw,tmp_path,mode):
    _,ws,_=gw
    SCRIPT.extend([{'tool_calls':[tool_call('write','write_file',path='new.txt',content='модель')]},{'content':'Ответ с метриками'}])
    proc=await launch(gw,tmp_path)
    try:
        _,events=await receive(proc,'ready')
        session=next(e['session']['id'] for e in events if e['type']=='snapshot')
        send(proc,type='submit',text='Первая задача')
        request,_=await receive(proc,'permission_request')
        send(proc,type='approve',id=request['id'],decision='allow')
        _,events=await receive(proc,'idle')
        assert any(e['type']=='assistant' and e['metrics']['completed_at'] for e in events)
        assert next(e['session']['title'] for e in events if e['type']=='snapshot')=='Первая задача'
        count=len(SEEN)
        send(proc,type='submit',text='/revert')
        _,events=await receive(proc,'idle')
        table=next(e for e in events if e['type']=='table')
        choice=table['selection']['choices'][0]
        send(proc,type='select',kind='revert',session_id=session,target=choice['target'])
        dialog,_=await receive(proc,'input_request')
        assert {c['value'] for c in dialog['choices']}=={'message','files'}
        assert [c['label'] for c in dialog['choices']]==['Исправить текст','Исправить текст и отменить все изменения']
        if mode=='cancel':
            send(proc,type='cancel')
        else:
            send(proc,type='answer',id=dialog['id'],value=mode)
        _,events=await receive(proc,'idle')
        assert len(SEEN)==count
        if mode=='cancel':
            assert not any(e['type']=='draft' for e in events)
        else:
            assert next(e['text'] for e in events if e['type']=='draft')=='Первая задача'
            assert next(e['history'] for e in events if e['type']=='snapshot')==[]
        assert (ws/'new.txt').exists() == (mode!='files')
    finally:
        await stop(proc)


async def test_dashboard_context_planning_mcp_without_model_then_tool_updates(gw, tmp_path):
    from tests.test_cli import MCP
    cfg, _, _ = gw
    config = json.loads(cfg.read_text())
    config['mcp'] = {'servers': {'local': {'type': 'stdio', 'command': sys.executable, 'args': [MCP]},
                                 'off': {'type': 'stdio', 'enabled': False, 'command': '/never-run'}}}
    cfg.write_text(json.dumps(config))
    SCRIPT.extend([{'tool_calls': [tool_call('g', 'goal', text='Проверить боковую панель'),
                                   tool_call('t', 'todo', items=[{'id': '1', 'text': 'Показать контекст', 'status': 'completed'}])]},
                   {'content': 'Готово'}])
    proc = await launch(gw, tmp_path)
    try:
        _, events = await receive(proc, 'ready')
        snapshot = next(e for e in events if e['type'] == 'snapshot')
        assert snapshot['context']['max_input_tokens'] == snapshot['context']['context_window'] - snapshot['context']['max_output_tokens']
        assert snapshot['context']['context_source'] == 'fallback'
        assert snapshot['context']['estimated_tokens'] > 0
        servers = {s['name']: s for s in snapshot['mcp']}
        assert servers['local']['state'] == 'connected' and servers['off']['state'] == 'disabled'
        assert not SEEN
        send(proc, type='submit', text='/mcp')
        _, menu_events = await receive(proc, 'idle')
        menu = next(event for event in menu_events if event['type'] == 'table')
        assert any(row[0] == 'off' and row[1] == 'выключен' for row in menu['rows'])
        send(proc, type='submit', text='Обнови цель и план')
        _, events = await receive(proc, 'idle')
        assert any(e['type'] == 'context' for e in events)
        dashboard = [e for e in events if e['type'] == 'dashboard'][-1]
        assert dashboard['planning']['goal'] == 'Проверить боковую панель'
        assert dashboard['planning']['todos'][0]['status'] == 'completed'
        assert any('Текущая цель и план' in (m.get('content') or '') for m in SEEN[-1]['body']['messages'])
    finally:
        await stop(proc)


async def test_switch_agent_without_model_and_queued_switch_waits_for_running_turn(gw, tmp_path):
    cfg, ws, db = gw
    config = json.loads(cfg.read_text()); config['permissions'] = {'mode': 'allow_all'}; cfg.write_text(json.dumps(config))
    SCRIPT.extend([{'tool_calls': [tool_call('q', 'question', question='Продолжить?', options=['Да', 'Нет'])]},
                   {'tool_calls': [tool_call('w', 'write_file', path='build-only', content='ok')]}, {'content': 'Готово'},
                   {'tool_calls': [tool_call('blocked', 'write_file', path='plan-write', content='no')]}, {'content': 'Только план'}])
    proc = await launch(gw, tmp_path)
    try:
        await receive(proc, 'ready')
        send(proc, type='set_agent', mode='plan')
        mode, events = await receive(proc, 'agent_mode')
        assert mode['mode'] == 'plan' and not SEEN
        snapshot = next(e for e in events if e['type'] == 'snapshot')
        assert snapshot['session']['agent_mode'] == 'plan'
        names = {row['name'] for row in snapshot['catalog'] if row['kind'] == 'tool'}
        assert 'question' in names and 'write_file' not in names
        send(proc, type='set_agent', mode='build'); await receive(proc, 'agent_mode')
        send(proc, type='submit', text='Построй файл')
        question, _ = await receive(proc, 'input_request')
        assert question['question'] and question['choices'][-1]['custom']
        send(proc, type='set_agent', mode='plan')
        mode, _ = await receive(proc, 'agent_mode')
        assert mode['mode'] == 'build' and mode['next_mode'] == 'plan'
        send(proc, type='answer', id=question['id'], value='Да')
        _, events = await receive(proc, 'idle')
        assert (ws / 'build-only').read_text() == 'ok'
        assert any(e['type'] == 'agent_mode' and e['mode'] == 'plan' and e['next_mode'] is None for e in events)
        send(proc, type='submit', text='Составь план')
        _, events = await receive(proc, 'idle')
        assert not (ws / 'plan-write').exists()
        assert any(e['type'] == 'tool_result' and not e['ok'] for e in events)
        from agent.store import SessionStore
        store = SessionStore(db); assert store.list('local')[0].agent_mode == 'plan'; store.close()
    finally:
        await stop(proc)


async def test_question_timeout_closes_input_and_continues(gw, tmp_path):
    cfg, _, _ = gw
    config = json.loads(cfg.read_text()); config['permissions'] = {'timeout': 1}; cfg.write_text(json.dumps(config))
    SCRIPT.extend([{'tool_calls': [tool_call('q', 'question', question='Уточнить? [y/N]')]}, {'content': 'Вопрос пропущен'}])
    proc = await launch(gw, tmp_path)
    try:
        await receive(proc, 'ready'); send(proc, type='submit', text='Начни')
        request, _ = await receive(proc, 'input_request')
        assert request['question'] and 'choices' not in request
        _, events = await receive(proc, 'idle')
        assert any(e['type'] == 'input_closed' and e['id'] == request['id'] for e in events)
        result = next(e for e in events if e['type'] == 'tool_result')
        assert json.loads(result['content']) == {'cancelled': True}
        assert any(e['type'] == 'done' and e['reason'] == 'stop' for e in events)
    finally:
        await stop(proc)


async def test_steering_enters_active_cycle_before_done_and_rejects_idle(gw, tmp_path):
    SCRIPT.extend([{'tool_calls': [tool_call('q', 'question', question='С чего начать?')]}, {'content': 'Уточнение учтено'}])
    proc = await launch(gw, tmp_path)
    try:
        await receive(proc, 'ready')
        send(proc, type='submit', text='Начни работу')
        question, _ = await receive(proc, 'input_request')
        send(proc, type='steer', id='live-input', text='Обязательно сохрани Unicode')
        queued, _ = await receive(proc, 'input_queued')
        assert queued['status'] == 'pending'
        send(proc, type='answer', id=question['id'], value='Сначала чтение')
        _, events = await receive(proc, 'idle')
        applied = next(e for e in events if e['type'] == 'input_applied')
        assert applied['id'] == 'live-input' and applied['request_id'] == queued['request_id']
        assert sum(e['type'] == 'done' for e in events) == 1
        assert any(m.get('content') == 'Обязательно сохрани Unicode' for m in SEEN[-1]['body']['messages'])
        snapshot = next(e for e in reversed(events) if e['type'] == 'snapshot')
        assert not snapshot['pending_inputs']
        send(proc, type='steer', id='too-late', text='Поздний текст')
        rejected, _ = await receive(proc, 'steering_rejected')
        assert rejected['text'] == 'Поздний текст'
    finally:
        await stop(proc)


async def test_pending_steering_survives_cancel_and_process_restart(gw, tmp_path):
    SCRIPT.extend([{'tool_calls': [tool_call('q', 'question', question='Жду ответа')]}])
    proc = await launch(gw, tmp_path)
    try:
        _, events = await receive(proc, 'ready')
        session = next(e['session']['id'] for e in events if e['type'] == 'snapshot')
        send(proc, type='submit', text='Начальная задача'); await receive(proc, 'input_request')
        send(proc, type='steer', id='durable-input', text='Не теряй это уточнение'); await receive(proc, 'input_queued')
        send(proc, type='cancel'); _, events = await receive(proc, 'idle')
        assert next(e for e in events if e['type'] == 'snapshot')['pending_inputs'][0]['id'] == 'durable-input'
    finally:
        await stop(proc)
    proc = await launch(gw, tmp_path)
    try:
        await receive(proc, 'ready')
        send(proc, type='submit', text='/resume ' + session)
        _, events = await receive(proc, 'idle')
        snapshot = next(e for e in events if e['type'] == 'snapshot')
        assert snapshot['pending_inputs'][0]['text'] == 'Не теряй это уточнение'
    finally:
        await stop(proc)


async def test_provider_wizard_edit_manage_and_cancel_are_interactive(gw, tmp_path):
    proc = await launch(gw, tmp_path)
    try:
        await receive(proc, 'ready')
        send(proc, type='submit', text='/provider')
        table, _ = await receive(proc, 'table')
        assert any(c.get('command') == '/provider add' for c in table['selection']['choices'])
        await receive(proc, 'idle')
        send(proc, type='submit', text='/provider add')
        for value in ('local', 'https://example.test/v1', 'none'):
            request, _ = await receive(proc, 'input_request')
            send(proc, type='answer', id=request['id'], value=value)
        _, events = await receive(proc, 'idle')
        assert not any(e['type'] == 'error' for e in events), events
        assert next(e['provider'] for e in events if e['type'] == 'snapshot') == 'local'
        send(proc, type='submit', text='/provider edit local')
        for value in ('https://changed.test/v1', 'bearer', 'wizard-hidden-key'):
            request, _ = await receive(proc, 'input_request')
            assert request['secret'] == (value == 'wizard-hidden-key')
            send(proc, type='answer', id=request['id'], value=value)
        _, events = await receive(proc, 'idle')
        assert not any(e['type'] == 'error' for e in events), events
        assert 'wizard-hidden-key' not in json.dumps(events)
        for action in ('disable', 'enable'):
            send(proc, type='submit', text=f'/provider {action} glm')
            _, events = await receive(proc, 'idle')
            assert not any(e['type'] == 'error' for e in events), events
        send(proc, type='submit', text='/provider remove glm')
        request, _ = await receive(proc, 'input_request')
        assert 'подключение glm' in request['prompt']
        send(proc, type='answer', id=request['id'], value='y')
        await receive(proc, 'idle')
        registry = tmp_path / 'agent-home/providers.json'
        assert 'glm' not in json.loads(registry.read_text())['providers']
        send(proc, type='submit', text='/provider add temporary')
        request, _ = await receive(proc, 'input_request')
        send(proc, type='answer', id=request['id'], value='https://example.test/v1')
        await receive(proc, 'input_request')
        send(proc, type='cancel')
        await receive(proc, 'idle')
        assert 'temporary' not in json.loads(registry.read_text())['providers']
        send(proc, type='submit', text='/provider disable local')
        _, events = await receive(proc, 'idle')
        assert any(e['type'] == 'error' and 'Сначала выберите' in e['message'] for e in events)
    finally:
        await stop(proc)


async def test_opencode_import_model_settings_and_export_dialogs(gw,tmp_path):
    source=tmp_path/'input.json';source.write_text(json.dumps({'model':'custom/chat','provider':{'custom':{
        'options':{'baseURL':'https://example.test/v1','apiKey':'ui-import-hidden-key'},'models':{'chat':{}}}}}))
    proc=await launch(gw,tmp_path)
    try:
        await receive(proc,'ready')
        send(proc,type='submit',text='/provider import')
        for value in (str(source),'n','y'):
            request,_=await receive(proc,'input_request')
            send(proc,type='answer',id=request['id'],value=value)
        _,events=await receive(proc,'idle')
        assert not any(e['type']=='error' for e in events),events
        assert next(e['provider'] for e in events if e['type']=='snapshot')=='custom'
        assert 'ui-import-hidden-key' not in json.dumps(events)
        send(proc,type='submit',text='/provider model custom chat')
        for value in ('Понятное имя','8192','256','.4','.8','low'):
            request,_=await receive(proc,'input_request')
            send(proc,type='answer',id=request['id'],value=value)
        _,events=await receive(proc,'idle')
        assert not any(e['type']=='error' for e in events),events
        assert next(e['context']['max_input_tokens'] for e in events if e['type']=='snapshot')==8192
        output=tmp_path/'output.json'
        send(proc,type='submit',text='/provider export')
        request,_=await receive(proc,'input_request');send(proc,type='answer',id=request['id'],value='custom')
        request,_=await receive(proc,'input_request');send(proc,type='answer',id=request['id'],value=str(output))
        _,events=await receive(proc,'idle')
        assert not any(e['type']=='error' for e in events),events
        assert 'ui-import-hidden-key' not in output.read_text()
        exported=json.loads(output.read_text())['provider']['custom']['models']['chat']
        assert exported['name']=='Понятное имя' and exported['limit']['context']==8448
    finally:await stop(proc)


async def test_permissions_picker_persists_session_saves_settings_and_blocks_writes(gw, tmp_path):
    cfg, ws, db = gw
    proc = await launch(gw, tmp_path)
    try:
        await receive(proc, 'ready')
        send(proc, type='submit', text='/permissions')
        _, rows = await receive(proc, 'idle')
        menu = next(event['selection'] for event in rows if event['type'] == 'table')
        assert menu['kind'] == 'permissions'
        assert {item['key'] for item in menu['choices']} == {'allow_all', 'default', 'read_only', 'save'}
        send(proc, type='submit', text='/permissions read_only save')
        _, rows = await receive(proc, 'idle')
        snap = next(event for event in rows if event['type'] == 'snapshot')
        assert snap['permissions_mode'] == 'read_only'
        assert snap['session']['permissions_mode'] == 'read_only'
        saved = json.loads((tmp_path / 'agent-home/settings.json').read_text())
        assert saved['permissions']['mode'] == 'read_only'
        SCRIPT.extend([{'tool_calls': [tool_call('blocked', 'write_file', path='blocked.txt', content='x')]}, {'content': 'Запись запрещена'}])
        send(proc, type='submit', text='Проверить запрет записи')
        _, rows = await receive(proc, 'idle')
        assert not any(event['type'] == 'permission_request' for event in rows)
        assert not (ws / 'blocked.txt').exists()
        result = next(event for event in rows if event['type'] == 'tool_result')
        assert not result['ok'] and result['decision'] == 'deny'
        from agent.store import SessionStore
        store = SessionStore(db)
        assert store.get(snap['session']['id']).permissions_mode == 'read_only'
        store.close()
    finally:
        await stop(proc)


async def test_cancel_command_after_completed_model_turn():
    from types import SimpleNamespace
    bridge = Bridge([], output=io.StringIO())
    completed = asyncio.create_task(asyncio.sleep(0))
    await completed
    bridge.app = SimpleNamespace(task=completed)
    started = asyncio.Event()
    async def command():
        started.set()
        await asyncio.Event().wait()
    bridge.work = asyncio.create_task(command())
    await started.wait()
    bridge.handle({'type': 'cancel'})
    with pytest.raises(asyncio.CancelledError):
        await bridge.work
    assert completed.done() and not completed.cancelled()
