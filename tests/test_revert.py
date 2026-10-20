"""Контрольные точки без Git, история веток, метрики и автоматические названия."""
import json
from pathlib import Path

import pytest

from agent.checkpoints import capture, plan, restore
from agent.runtime import make_agent
from agent.settings import DEFAULTS, merge
from agent.store import SessionStore
from tests.fakes import tool_call
from tests.test_loop import make_agent as fixture_agent, run


def checkpoint(store, session, request, action, text='задача'):
    before=capture(session.workspace, [store.path,store.path+'-wal',store.path+'-shm'])
    store.checkpoint(session.id,request,before=before)
    seq=store.add_message(session.id,{'role':'user','content':text}, {'request_id':request,'user_input':text})
    action()
    store.add_message(session.id,{'role':'assistant','content':'готово'}, {'request_id':request})
    store.checkpoint(session.id,request,after=capture(session.workspace,[store.path,store.path+'-wal',store.path+'-shm']))
    return seq


def setup_store(tmp_path):
    workspace=tmp_path/'workspace';workspace.mkdir()
    store=SessionStore(tmp_path/'sessions.db')
    return store,store.create('local',workspace=str(workspace)),workspace


def test_snapshot_objects_deduplicate_and_keep_branch(tmp_path):
    store,session,ws=setup_store(tmp_path)
    (ws/'existing').write_text('ручные изменения до агента')
    first=checkpoint(store,session,'r1',lambda:(ws/'existing').write_text('ответ 1'))
    checkpoint(store,session,'r2',lambda:(ws/'second').write_bytes(b'\x00binary'))
    assert store.db.execute('SELECT COUNT(*) FROM agent_snapshot_blobs').fetchone()[0]==3
    assert store.db.execute("SELECT parent_request_id FROM agent_checkpoints WHERE request_id='r2'").fetchone()[0]=='r1'
    revision=store.revision(session.id)
    reverted=store.rewind(session.id,first,revision=revision,files=True)
    assert reverted['text']=='задача'
    assert (ws/'existing').read_text()=='ручные изменения до агента'
    assert not (ws/'second').exists() and not store.messages(session.id)
    assert store.export(session.id)['reverts']
    checkpoint(store,session,'new-branch',lambda:(ws/'third').write_text('ветка'))
    assert store.db.execute("SELECT parent_request_id FROM agent_checkpoints WHERE request_id='new-branch'").fetchone()[0]==''
    assert store.db.execute('SELECT COUNT(*) FROM agent_checkpoints').fetchone()[0]==3
    store.delete(session.id)
    assert store.db.execute('SELECT COUNT(*) FROM agent_snapshot_blobs').fetchone()[0]==0
    store.close()


def test_message_only_keeps_files_and_rejects_stale_menu(tmp_path):
    store,session,ws=setup_store(tmp_path)
    seq=checkpoint(store,session,'r1',lambda:(ws/'x').write_text('сохранить'))
    old=store.revision(session.id)
    store.add_message(session.id,{'role':'user','content':'ещё'})
    with pytest.raises(ValueError,match='история изменилась'):
        store.rewind(session.id,seq,revision=old)
    store.rewind(session.id,seq,revision=store.revision(session.id))
    assert (ws/'x').read_text()=='сохранить' and not store.messages(session.id)
    store.close()


def test_conflict_preflight_never_removes_other_files(tmp_path):
    store,session,ws=setup_store(tmp_path)
    (ws/'changed').write_text('исходный')
    def change():
        (ws/'created').write_text('модель')
        (ws/'changed').write_text('изменение модели')
    seq=checkpoint(store,session,'r',change)
    (ws/'changed').write_text('новое ручное изменение')
    with pytest.raises(ValueError,match='конфликт'):
        store.rewind(session.id,seq,revision=store.revision(session.id),files=True)
    assert (ws/'created').read_text()=='модель'
    assert (ws/'changed').read_text()=='новое ручное изменение'
    assert len(store.messages(session.id))==2
    store.close()


def test_created_directory_does_not_remove_new_manual_files(tmp_path):
    store,session,ws=setup_store(tmp_path)
    def change():
        (ws/'new').mkdir();(ws/'new/model').write_text('модель')
    seq=checkpoint(store,session,'r',change)
    (ws/'new/manual').write_text('ручной')
    with pytest.raises(ValueError,match='новые файлы'):
        store.rewind(session.id,seq,revision=store.revision(session.id),files=True)
    assert (ws/'new/model').exists() and (ws/'new/manual').exists()
    store.close()


def test_old_messages_editable_without_snapshot(tmp_path):
    store,session,ws=setup_store(tmp_path)
    seq=store.add_message(session.id,{'role':'user','content':'старый текст'})
    with pytest.raises(ValueError,match='нет снимков'):
        store.revert_checkpoints(session.id,seq)
    assert store.rewind(session.id,seq,revision=store.revision(session.id))['text']=='старый текст'
    store.close()


async def test_turn_checkpoint_covers_shell_and_replays_metrics(tmp_path):
    import httpx
    from agent.llm import LLMClient
    from tests.fakes import scripted_transport
    store,session,ws=setup_store(tmp_path)
    (ws/'old.txt').write_text('старое')
    script=[{'tool_calls':[tool_call('shell-call','shell',command="printf новое > old.txt; mkdir folder; printf data > folder/new.txt")]},{'content':'Ответ'}]
    async with httpx.AsyncClient(base_url='http://fixture/v1',transport=scripted_transport(script)) as http:
        agent=make_agent(llm=LLMClient(http),settings=merge(DEFAULTS,{'permissions':{'mode':'allow_all'}}),store=store,session=session)
        events=await run(agent,'Обнови файлы')
    replies=[event for event in events if event['type']=='assistant']
    assert len(replies)==2 and all(event['metrics']['completed_at'] for event in replies)
    assert all(event['metrics']['duration_ms']>=0 and event['metrics']['tokens_per_second']>0 for event in replies)
    assert 'metrics' not in store.context_messages(session.id)[-1]
    assert store.timeline(session.id)[-1]['metadata']['metrics']==replies[-1]['metrics']
    store.rewind(session.id,1,revision=store.revision(session.id),files=True)
    assert (ws/'old.txt').read_text()=='старое' and not (ws/'folder').exists()
    store.close()


async def test_titles_generated_locally_and_custom_preserved(tmp_path):
    agent,store,_=fixture_agent(tmp_path,[{'content':'готово'}])
    assert agent.session.title.startswith('Новая сессия ·')
    await run(agent,'## Настройка мониторинга сервера\nПодробности')
    assert store.get(agent.session.id).title=='Настройка мониторинга сервера'
    store.update(agent.session.id,title='Моё название',title_auto=0)
    agent.session=store.get(agent.session.id)
    await run(agent,'другая задача')
    assert store.get(agent.session.id).title=='Моё название'
    await agent.llm.http.aclose();store.close()
