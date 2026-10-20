"""JSONL-мост проверяется отдельным процессом против настоящего локального шлюза."""
import asyncio
import io
import json
import os
import shutil
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


async def choose_command(proc, label=None):
    _, events = await receive(proc, 'idle')
    menu = next(event['selection'] for event in events if event['type'] == 'table')
    assert menu['kind'] == 'command'
    choice = next(item for item in menu['choices'] if item['label'] == label) if label else menu['choices'][0]
    send(proc, type='select', kind='command', target=choice['target'], session_id=menu['session_id'])
    return menu


async def stop(proc):
    if proc.returncode is None:
        send(proc, type='shutdown')
        await asyncio.wait_for(proc.wait(), 5)
    assert proc.returncode == 0, await proc.stderr.read()


async def test_every_catalog_command_opens_selection_without_model_or_credentials(gw, tmp_path):
    proc = await launch(gw, tmp_path, {'GW_TOKEN': '', 'ZAI_API_KEY': '', 'ZAI_CODING_API_KEY': '',
                                      'MISTRAL_API_KEY': '', 'ZHIPU_API_KEY': ''})
    try:
        _, events = await receive(proc, 'ready')
        snapshot = next(event for event in events if event['type'] == 'snapshot')
        session = snapshot['session']['id']
        commands = [item['command'] for item in snapshot['catalog']]
        assert len(commands) == 13 and '/providers' in commands and '/permissions' in commands
        assert all(item.get('label') and item['kind'] == 'command' for item in snapshot['catalog'])
        assert not any(command.startswith('/provider ') for command in commands)
        for command in commands:
            send(proc, type='submit', text=command)
            _, events = await receive(proc, 'idle')
            assert not any(event['type'] in ('error', 'input_request') for event in events), (command, events)
            menu = next((event for event in events if event['type'] == 'table'), None)
            assert menu is not None and menu.get('selection'), (command, events)
            assert menu['selection']['session_id'] == session
        assert not any('messages' in row.get('body', {}) for row in SEEN), 'меню не должны вызывать модель'
    finally:
        await stop(proc)


async def test_command_menu_validates_selection_and_executes_only_after_choice(gw, tmp_path):
    proc = await launch(gw, tmp_path)
    try:
        _, events = await receive(proc, 'ready')
        original = next(event['session']['id'] for event in events if event['type'] == 'snapshot')
        send(proc, type='submit', text='/new')
        _, events = await receive(proc, 'idle')
        menu = next(event['selection'] for event in events if event['type'] == 'table')
        assert menu['session_id'] == original
        send(proc, type='select', kind='command', target='поддельный пункт', session_id=original)
        _, events = await receive(proc, 'idle')
        assert any(event['type'] == 'error' for event in events)
        send(proc, type='select', kind='command', target=menu['choices'][0]['target'], session_id=original)
        _, events = await receive(proc, 'idle')
        current = next(event['session']['id'] for event in events if event['type'] == 'snapshot')
        assert current != original
        send(proc, type='select', kind='command', target=menu['choices'][0]['target'], session_id=original)
        _, events = await receive(proc, 'idle')
        assert any(event['type'] == 'error' and 'устарел' in event['message'] for event in events)
    finally:
        await stop(proc)


async def test_compact_notices_survive_snapshot_and_failure_can_be_retried(gw, tmp_path):
    cfg, _, _ = gw
    settings = json.loads(cfg.read_text())
    settings['model_retry'] = {'initial_delay': 0, 'max_delay': 0}
    cfg.write_text(json.dumps(settings))
    SCRIPT.extend([{'content': 'Старый ответ ' * 500}, {'status': 401, 'error': 'нет доступа'},
                   {'status': 502, 'error': 'временная ошибка'},
                   {'content': 'Сводка: проверить проект'}, {'content': 'Продолжение'}])
    proc = await launch(gw, tmp_path)
    try:
        await receive(proc, 'ready')
        send(proc, type='submit', text='Проверить проект')
        await receive(proc, 'idle')
        send(proc, type='submit', text='/compact')
        await choose_command(proc, 'Составить сводку текущей моделью')
        _, events = await receive(proc, 'idle')
        assert any(e['type'] == 'notice' and 'Суммаризация' in e['message'] for e in events)
        assert any(e['type'] == 'notice' and e.get('level') == 'error' and '401' in e['message'] for e in events)
        snapshot = next(e for e in events if e['type'] == 'snapshot')
        assert 'history' not in snapshot
        assert snapshot['session']['context_seq'] == 0
        send(proc, type='submit', text='/compact')
        await choose_command(proc, 'Составить сводку текущей моделью')
        _, events = await receive(proc, 'idle')
        assert any(e['type'] == 'notice' and 'сводка создана моделью' in e['message'] for e in events)
        retry = next(e for e in events if e.get('retry'))
        assert retry['type'] == 'warning'
        assert retry['retry']['purpose'] == 'compaction'
        assert retry['retry']['attempt'] == 1 and retry['retry']['status'] == 502
        snapshot = next(e for e in events if e['type'] == 'snapshot')
        assert 'history' not in snapshot and snapshot['session']['context_seq'] > 0
        send(proc, type='submit', text='Продолжи')
        await receive(proc, 'idle')
        assert 'Сводка: проверить проект' in SEEN[-1]['body']['messages'][0]['content']
    finally:
        await stop(proc)


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
            assert table.get('selection') and isinstance(table['rows'],list)
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
        send(proc,type='submit',text='/connect gateway')
        event,_=await receive(proc,'input_request')
        assert event['prompt']
        assert 'URL API' in event['prompt']
        send(proc,type='answer',id=event['id'],value='')
        event,_=await receive(proc,'input_request')
        assert [choice['value'] for choice in event.get('choices',[])]==['bearer','mtls','basic','none','env']
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


def test_launcher_refreshes_sources_without_reinstall(monkeypatch,tmp_path):
    source=tui.assets()
    monkeypatch.setenv('GWAGENT_HOME',str(tmp_path/'home'))
    target=tmp_path/'home/cache/tui/deps'
    (target/'node_modules/@opentui/core').mkdir(parents=True)
    (target/'node_modules/@opentui/core/package.json').write_text('{}')
    for name in ('package.json','bun.lock'):
        shutil.copy2(source/name,target/name)
    assert tui.installed(source)==target
    (target/'src').mkdir()
    (target/'src/main.ts').write_text('устаревший интерфейс')
    tui.sync(source,target)
    assert (target/'src/main.ts').read_text()==(source/'src/main.ts').read_text()


def test_launcher_does_not_check_bun_version(monkeypatch,capsys,tmp_path):
    monkeypatch.setenv('GWAGENT_HOME',str(tmp_path/'home'))
    monkeypatch.setattr(sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(sys.stdout,'isatty',lambda:True)
    monkeypatch.setattr(tui,'bun_binary',lambda:None)
    assert tui.dispatch(['--tui'])==2
    error=capsys.readouterr().err
    assert '1.3.8' not in error and 'Bun' in error
    monkeypatch.setattr(tui,'bun_binary',lambda:'/mock/bun')
    assert tui.dispatch(['--tui'])==2
    error=capsys.readouterr().err
    assert '1.3.8' not in error and 'gwagent tui install' in error


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
        send(proc,type='submit',text='/connect gateway')
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
        assert len(commands) == 13 and '/providers' in commands and '/tools' in commands
        assert names == []
        send(proc, type='submit', text='/tools')
        _, rows = await receive(proc, 'idle')
        names = [item['key'] for item in next(event['selection']['choices'] for event in rows if event['type'] == 'table')]
        assert 'read_file' in names and 'mcp__test__echo' in names and 'shell' not in names
        assert not SEEN
        for name in ('read_file','mcp__test__echo'):
            send(proc,type='submit',text='/tools '+name)
            _,events=await receive(proc,'idle')
            menu = next(e['selection'] for e in events if e['type'] == 'table')
            assert menu['kind'] == 'command' and name in menu['title']
            description = next(item for item in menu['choices'] if item['label'] == 'Описание')
            send(proc, type='select', kind='command', target=description['target'], session_id=menu['session_id'])
            _, events = await receive(proc, 'idle')
            assert any(e['type'] == 'notice' and description['description'] in e['message'] for e in events)
            assert not any(e['type'] in ('tool_call','permission_request','user') for e in events)
        send(proc, type='submit', text='/mcp test')
        _, events = await receive(proc, 'idle')
        menu = next(event['selection'] for event in events if event['type'] == 'table')
        assert menu['kind'] == 'command'
        tool = next(item for item in menu['choices'] if item['label'] == 'mcp__test__echo')
        send(proc, type='select', kind='command', target=tool['target'], session_id=menu['session_id'])
        _, events = await receive(proc, 'idle')
        assert any('mcp__test__echo' in event.get('selection', {}).get('title', '') for event in events)
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
        menu=await choose_command(proc,'Посмотреть')
        assert {item['label'] for item in menu['choices']} >= {'Посмотреть','Добавить','Изменить','Удалить'}
        await choose_command(proc,'first')
        await choose_command(proc,'Использовать')
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
        await choose_command(proc, 'Посмотреть и продолжить')
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
        assert len(snapshot['history']) == 1
        assert snapshot['history'][0] == {'role': 'user', 'content': 'Сохранённая история', 'message_id': 'seq:1'}
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
        await choose_command(proc, 'Изменить сообщение или откатить')
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
        send(proc, type='submit', text='/tools')
        _, events = await receive(proc, 'idle')
        names = {item['key'] for item in next(e['selection']['choices'] for e in events if e['type'] == 'table')}
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


async def test_provider_section_navigation_back_reopens_window_and_rejects_stale_route(gw, tmp_path):
    home = tmp_path / 'agent-home'
    home.mkdir(parents=True, exist_ok=True)
    (home / 'providers.json').write_text(json.dumps({'default': 'first', 'selected': False, 'providers': {
        'first': {'url': 'https://example.test/v1', 'token_env': 'GW_TOKEN', 'model': 'alpha', 'models': []}}}))
    proc = await launch(gw, tmp_path)
    try:
        await receive(proc, 'ready')
        send(proc, type='submit', text='/providers')
        menu = await choose_command(proc, 'Посмотреть')
        assert menu['route'] == '/providers'
        await choose_command(proc, 'first')
        _, events = await receive(proc, 'idle')
        page = next(e['selection'] for e in events if e['type'] == 'table')
        assert page['route'] == '/_ui/provider/view first'
        send(proc, type='navigate', route=page['route'], session_id=page['session_id'])
        _, events = await receive(proc, 'idle')
        again = next(e['selection'] for e in events if e['type'] == 'table')
        assert again['route'] == page['route']
        assert [c['label'] for c in again['choices']] == ['Сведения', 'Использовать', 'Дополнительно']
        send(proc, type='navigate', route='/providers/unknown', session_id=again['session_id'])
        _, events = await receive(proc, 'idle')
        assert any(e['type'] == 'error' and 'устарело' in e['message'] for e in events)
    finally:
        await stop(proc)


async def test_provider_section_pages_add_edit_and_remove(gw, tmp_path):
    proc = await launch(gw, tmp_path)
    try:
        await receive(proc, 'ready')
        send(proc, type='submit', text='/providers')
        await choose_command(proc, 'Добавить')
        menu = await choose_command(proc, 'Base URL и токен')
        assert {c['label'] for c in menu['choices']} == {'Base URL и токен', 'Base URL и сертификат'}
        for value in ('second', 'https://example.test/v1', 'token-1'):
            request, _ = await receive(proc, 'input_request')
            send(proc, type='answer', id=request['id'], value=value)
        _, events = await receive(proc, 'idle')
        assert not any(e['type'] == 'error' for e in events), events
        assert 'token-1' not in json.dumps(events)
        registry = json.loads((tmp_path / 'agent-home/providers.json').read_text())
        assert registry['providers']['second']['url'] == 'https://example.test/v1'
        send(proc, type='submit', text='/providers')
        await choose_command(proc, 'Изменить')
        await choose_command(proc, 'second')
        menu = await choose_command(proc, 'Заменить Base URL')
        assert {c['label'] for c in menu['choices']} == {'Заменить Base URL', 'Заменить токен'}
        request, _ = await receive(proc, 'input_request')
        assert request['prompt'].startswith('Новый Base URL')
        send(proc, type='answer', id=request['id'], value='https://changed.test/v1')
        _, events = await receive(proc, 'idle')
        assert not any(e['type'] == 'error' for e in events), events
        registry = json.loads((tmp_path / 'agent-home/providers.json').read_text())
        assert registry['providers']['second']['url'] == 'https://changed.test/v1'
        send(proc, type='submit', text='/providers')
        await choose_command(proc, 'Удалить')
        await choose_command(proc, 'second')
        request, _ = await receive(proc, 'input_request')
        assert 'подключение second' in request['prompt']
        send(proc, type='answer', id=request['id'], value='y')
        _, events = await receive(proc, 'idle')
        assert not any(e['type'] == 'error' for e in events), events
        registry = json.loads((tmp_path / 'agent-home/providers.json').read_text())
        assert 'second' not in registry['providers']
    finally:
        await stop(proc)


async def test_provider_wizard_edit_manage_and_cancel_are_interactive(gw, tmp_path):
    proc = await launch(gw, tmp_path)
    try:
        await receive(proc, 'ready')
        send(proc, type='submit', text='/provider')
        table, _ = await receive(proc, 'table')
        assert [c['label'] for c in table['selection']['choices']] == ['Посмотреть', 'Добавить', 'Изменить', 'Удалить']
        await receive(proc, 'idle')
        send(proc, type='submit', text='/provider add')
        await choose_command(proc)
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
        await choose_command(proc)
        for value in (str(source),'n','y'):
            request,_=await receive(proc,'input_request')
            send(proc,type='answer',id=request['id'],value=value)
        _,events=await receive(proc,'idle')
        assert not any(e['type']=='error' for e in events),events
        assert next(e['provider'] for e in events if e['type']=='snapshot')=='custom'
        assert 'ui-import-hidden-key' not in json.dumps(events)
        send(proc,type='submit',text='/provider model custom chat')
        for value in ('Понятное имя','8448','8192','256','.4','.8','low'):
            request,_=await receive(proc,'input_request')
            send(proc,type='answer',id=request['id'],value=value)
        _,events=await receive(proc,'idle')
        assert not any(e['type']=='error' for e in events),events
        assert next(e['context']['max_input_tokens'] for e in events if e['type']=='snapshot')==8192
        output=tmp_path/'output.json'
        send(proc,type='submit',text='/provider export')
        await choose_command(proc)
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


async def test_provider_missing_arguments_and_management_menu_preserve_session(gw, tmp_path):
    proc = await launch(gw, tmp_path)
    try:
        _, events = await receive(proc, 'ready')
        original = next(event['session']['id'] for event in events if event['type'] == 'snapshot')
        send(proc, type='submit', text='/provider edit')
        await choose_command(proc, 'glm')
        request, _ = await receive(proc, 'input_request')
        send(proc, type='answer', id=request['id'], value='')
        request, _ = await receive(proc, 'input_request')
        assert request['default_value'] == 'env'
        send(proc, type='answer', id=request['id'], value='env')
        request, _ = await receive(proc, 'input_request')
        send(proc, type='answer', id=request['id'], value='ZAI_API_KEY')
        _, events = await receive(proc, 'idle')
        assert not any(event['type'] == 'error' for event in events)
        assert next(event['session']['id'] for event in events if event['type'] == 'snapshot') == original
        send(proc, type='submit', text='/provider manage')
        await choose_command(proc, 'glm')
        _, events = await receive(proc, 'idle')
        menu = next(event['selection'] for event in events if event['type'] == 'table')
        assert menu['kind'] == 'provider-actions'
        action = next(item for item in menu['choices'] if item['key'] == 'disable')
        assert not action['disabled']
        send(proc, type='submit', text=action['command'])
        _, events = await receive(proc, 'idle')
        assert not any(event['type'] == 'error' for event in events)
        data = json.loads((tmp_path/'agent-home/providers.json').read_text())
        assert not data['providers']['glm']['enabled']
        send(proc, type='submit', text='/provider enable')
        await choose_command(proc, 'glm')
        _, events = await receive(proc, 'idle')
        assert not any(event['type'] == 'error' for event in events)
        send(proc, type='submit', text='/provider edit a b')
        _, events = await receive(proc, 'idle')
        assert any(event['type'] == 'error' and 'аргументы' in event['message'] for event in events)
        send(proc, type='submit', text='/provider list')
        await receive(proc, 'idle')
    finally:
        await stop(proc)


async def test_input_history_and_click_edit_protocol(gw, tmp_path):
    proc = await launch(gw, tmp_path)
    try:
        _, events = await receive(proc, 'ready')
        session = next(e['session']['id'] for e in events if e['type'] == 'snapshot')
        SCRIPT.append({'content': 'Готово'})
        send(proc, type='submit', text='Первая строка\nВторая строка')
        _, events = await receive(proc, 'idle')
        user = next(e for e in events if e['type'] == 'user')
        send(proc, type='input_history', id='h1', session_id=session, direction='previous')
        history, _ = await receive(proc, 'input_history')
        assert history['message_id'] == user['message_id']
        assert history['text'] == 'Первая строка\nВторая строка'
        send(proc, type='input_history', id='h2', session_id=session, direction='next', cursor=history['message_id'])
        boundary, _ = await receive(proc, 'input_history')
        assert boundary['boundary'] == 'draft'
        send(proc, type='input_history', id='h3', session_id='чужая', direction='previous')
        error, _ = await receive(proc, 'input_history')
        assert error['id'] == 'h3' and error['error']
        send(proc, type='edit_message', session_id=session, message_id=user['message_id'])
        dialog, _ = await receive(proc, 'input_request')
        assert dialog['choices'][0]['value'] == 'message'
        send(proc, type='answer', id=dialog['id'], value='message')
        _, events = await receive(proc, 'idle')
        assert next(e['text'] for e in events if e['type'] == 'draft') == history['text']
        send(proc, type='edit_message', session_id=session, message_id='несуществующее')
        _, events = await receive(proc, 'idle')
        assert any(e['type'] == 'error' for e in events)
    finally:
        await stop(proc)


def test_history_full_text_pending_and_boundaries(tmp_path):
    from types import SimpleNamespace
    from agent.store import SessionStore
    from agent.cli import App, OWNER
    store = SessionStore(tmp_path / 'history.db')
    try:
        session = store.create(OWNER, workspace=str(tmp_path))
        original = 'Полный исходный текст\n' + 'я' * 22000
        store.add_message(session.id, {'role': 'user', 'content': 'Текст после хука'},
                          {'input_id': 'original', 'user_input': original})
        store.add_message(session.id, {'role': 'user', 'content': 'Служебное сообщение'}, {'request_id': 'r'})
        app = SimpleNamespace(session=session, store=store)
        app.revert_messages = lambda: App.revert_messages(app)
        bridge = Bridge.__new__(Bridge)
        bridge.ready, bridge.app = True, app
        events = []
        bridge.emit = events.append
        bridge.history_input({'id': '1', 'session_id': session.id, 'direction': 'previous'})
        assert events[-1]['text'] == original and events[-1]['message_id'] == 'original'
        bridge.history_input({'id': '2', 'session_id': session.id, 'direction': 'previous', 'cursor': 'original'})
        assert events[-1]['boundary'] == 'oldest'
        store.enqueue_input(session.id, 'request', 'pending', 'Ожидающее уточнение')
        bridge.history_input({'id': 'pending', 'session_id': session.id, 'direction': 'previous'})
        assert events[-1]['message_id'] == 'pending' and events[-1]['text'] == 'Ожидающее уточнение'
        store.reject_input('pending')
        store.add_message(session.id, {'role': 'user', 'content': 'я' * 300000})
        bridge.history_input({'id': '3', 'session_id': session.id, 'direction': 'previous'})
        assert 'слишком большое' in events[-1]['error'] and 'text' not in events[-1]
    finally:
        store.close()


async def test_click_edit_busy_guard_keeps_active_request(gw, tmp_path):
    proc = await launch(gw, tmp_path)
    try:
        _, events = await receive(proc, 'ready')
        session = next(e['session']['id'] for e in events if e['type'] == 'snapshot')
        SCRIPT.append({'content': 'Готово'})
        send(proc, type='submit', text='Запрос')
        user, _ = await receive(proc, 'user')
        send(proc, type='edit_message', session_id=session, message_id=user['message_id'])
        notice, events = await receive(proc, 'notice')
        assert 'после завершения' in notice['message']
        assert not any(e['type'] == 'idle' for e in events)
        await receive(proc, 'idle')
    finally:
        await stop(proc)


async def test_statistics_and_session_names_without_model(gw, tmp_path):
    proc = await launch(gw, tmp_path, {'GW_TOKEN': ''})
    try:
        await receive(proc, 'ready')
        send(proc, type='submit', text='/stats')
        await choose_command(proc, 'Текущая сессия')
        _, events = await receive(proc, 'idle')
        stats = next(e for e in events if e['type'] == 'table' and e.get('title') == 'Статистика')
        assert ['Всего токенов', '0'] in stats['rows']
        send(proc, type='submit', text='/rename')
        await choose_command(proc, 'Переименовать')
        question, _ = await receive(proc, 'input_request')
        send(proc, type='answer', id=question['id'], value='Понятное название проекта')
        _, events = await receive(proc, 'idle')
        snapshot = next(e for e in events if e['type'] == 'snapshot')
        assert snapshot['session']['title'] == 'Понятное название проекта'
        assert snapshot['session']['title_auto'] is False
        send(proc, type='submit', text='/sessions')
        await choose_command(proc, 'Посмотреть и продолжить')
        _, events = await receive(proc, 'idle')
        sessions = next(e['selection'] for e in events if e['type'] == 'table')
        current = next(item for item in sessions['choices'] if item['current'])
        assert current['title'] == 'Понятное название проекта'
        assert current['messages'] == current['tokens'] == 0
    finally:
        await stop(proc)
