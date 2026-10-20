"""Reasoning, корреляция и ограниченные сабагенты — только локальные сценарии."""

import asyncio
import json

import httpx
import pytest

from agent.llm import LLMClient
from agent.tools.subagent import Subagent
from agent.tracing import current
from tests.fakes import sse_body, tool_call
from tests.test_loop import make_agent, run


@pytest.mark.parametrize('streaming', [True, False])
async def test_provider_reasoning_roundtrip(tmp_path, streaming):
    agent, store, _ = make_agent(tmp_path, [])
    captured = []
    def handle(request):
        captured.append(request)
        if streaming:
            chunks = [{'choices': [{'delta': {'reasoning_content': 'Сравню '}}]},
                      {'choices': [{'delta': {'reasoning': 'варианты'}}]},
                      {'choices': [{'delta': {'content': 'Ответ'}, 'finish_reason': 'stop'}]}]
            body = ''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
            return httpx.Response(200, content=body, headers={'content-type':'text/event-stream'})
        return httpx.Response(200, json={'choices':[{'message':{'content':'Ответ','reasoning_content':'Сравню варианты'},'finish_reason':'stop'}]})
    agent.llm = LLMClient(httpx.AsyncClient(base_url='http://fixture/v1', transport=httpx.MockTransport(handle)))
    events = await run(agent)
    assert ''.join(e['text'] for e in events if e['type']=='reasoning') == 'Сравню варианты'
    assert store.messages(agent.session.id)[-1]['reasoning_content'] == 'Сравню варианты'
    rid = events[-1]['request_id']
    assert captured[0].headers['x-request-id'] == rid
    logs = store.traces(agent.session.id)
    assert next(x for x in logs if x['event']=='model.start')['span_id'] == captured[0].headers['x-span-id']
    assert all(x['request_id']==rid for x in logs)
    assert 'Сравню варианты' not in json.dumps(logs, ensure_ascii=False)
    assert current.get() is None
    await agent.llm.http.aclose()
    store.close()


async def test_child_permissions_budget_and_trace_tree(tmp_path):
    requests = []
    script = [{'tool_calls':[tool_call('delegate', 'subagent', task='Найди результат')]},
              {'tool_calls':[tool_call('danger-child', 'danger', target='x')]},
              {'content':'Дочерний ответ'}, {'content':'Итог'}]
    approvals = []
    async def approve(event):
        approvals.append(event['name'])
        return 'allow'
    agent, store, usage = make_agent(tmp_path, [], approver=approve)
    agent.registry.add(Subagent())
    def handle(request):
        requests.append((request.headers, json.loads(request.content)))
        return httpx.Response(200, content=sse_body(script.pop(0)),headers={'content-type':'text/event-stream'})
    agent.llm = LLMClient(httpx.AsyncClient(base_url='http://fixture/v1', transport=httpx.MockTransport(handle)))
    events = await run(agent)
    assert approvals == ['subagent','danger']
    assert events[-1]['reason']=='stop' and len(usage)==4
    assert len({r[0]['x-request-id'] for r in requests})==1
    assert len({r[0]['x-span-id'] for r in requests})==4
    assert all(t['function']['name']!='subagent' for t in requests[1][1]['tools'])
    child = next(e['session_id'] for e in events if e['type']=='subagent_start')
    assert store.messages(child)[0]['content']=='Найди результат'
    assert store.messages(agent.session.id)[0]['content']=='задача'
    logs = store.traces(agent.session.id, events[-1]['request_id'])
    assert {x['session_id'] for x in logs}=={child,agent.session.id}
    assert len([x for x in logs if x['event']=='model.end'])==4
    assert all(x['parent_span_id'] for x in logs if x['event']=='model.start')
    assert store.export(agent.session.id)['traces']==logs
    assert current.get() is None
    await agent.llm.http.aclose()
    store.close()


async def test_subagent_limit_and_disabled(tmp_path):
    agent, store, _ = make_agent(tmp_path, [{'tool_calls':[tool_call('a','subagent',task='один'),tool_call('b','subagent',task='два')]},
                                          {'content':'первый'}, {'content':'итог'}],
                                 subagents={'max_calls':1}, permissions={'mode':'allow_all'})
    agent.registry.add(Subagent())
    events = await run(agent)
    results = [e for e in events if e['type']=='tool_result']
    assert results[0]['ok'] and not results[1]['ok']
    assert 'лимит' in results[1]['content']
    agent.settings['subagents']['enabled']=False
    events = [e async for e in agent.run('ещё', delegate=True)]
    assert not next(e for e in events if e['type']=='tool_result')['ok']
    await agent.llm.http.aclose()
    store.close()


async def test_close_child_repairs_both_histories(tmp_path):
    agent, store, _ = make_agent(tmp_path, [{'tool_calls':[tool_call('pending','slow')]}], permissions={'mode':'allow_all'})
    agent.registry.add(Subagent())
    stream = agent.run('поработай', delegate=True)
    child_id = None
    async for event in stream:
        if event['type']=='subagent_start':
            child_id = event['session_id']
        if event['type']=='subagent_event' and event['event']['type']=='tool_call':
            await stream.aclose()
            break
    assert child_id
    for sid in (agent.session.id, child_id):
        assert store.messages(sid)[-1]['role']=='tool'
        assert store.messages(sid)[-1]['content']=='Отменено пользователем'
    assert not store.db.execute("SELECT 1 FROM agent_turns WHERE state='running'").fetchone()
    assert current.get() is None
    await agent.llm.http.aclose()
    store.close()


async def test_request_ids_unique_errors_correlated(tmp_path):
    agent, store, _ = make_agent(tmp_path, [{'status':429,'error':'лимит'},{'content':'готово'}], model_retry={'max_retries':0})
    first, second = await run(agent), await run(agent)
    assert first[-1]['request_id']!=second[-1]['request_id']
    logs = store.traces(agent.session.id, first[-1]['request_id'])
    assert next(x for x in logs if x['event']=='model.end')['data']['status']==429
    assert next(x for x in logs if x['event']=='request.end')['data']['state']=='error'
    await agent.llm.http.aclose()
    store.close()


def test_v2_database_migration_preserves_audit(tmp_path):
    import sqlite3
    from agent.store import MIGRATIONS, SCHEMA_VERSION, SessionStore
    path = tmp_path/'old.db'
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE agent_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);')
    for version in (1,2):
        db.executescript(MIGRATIONS[version])
    db.execute("INSERT INTO agent_meta VALUES ('schema_version','2')")
    db.execute("INSERT INTO agent_sessions (id,owner,created,updated) VALUES ('old','local',1,1)")
    db.execute("INSERT INTO agent_tool_runs VALUES ('old','call','read_file','{}','allow',1,3,1)")
    db.commit();db.close()
    store = SessionStore(path)
    assert store.version()==SCHEMA_VERSION
    assert store.get("old").permissions_mode == ""
    assert store.tool_runs('old')[0]['request_id']==''
    store.trace('request','span','','old','model.end',{'completion_tokens':3})
    assert store.traces('old')[0]['data']['completion_tokens']==3
    store.close()


async def test_child_timeout_closes_tool_and_finishes_trace(tmp_path):
    agent, store, _ = make_agent(tmp_path, [{'tool_calls':[tool_call('slow-child','slow')]}],
                                 subagents={'timeout':0.05}, permissions={'mode':'allow_all'})
    agent.registry.add(Subagent())
    events = [event async for event in agent.run('таймаут',delegate=True)]
    end = next(e for e in events if e['type']=='subagent_end')
    assert end['reason']=='timeout'
    assert not next(e for e in events if e['type']=='tool_result')['ok']
    assert store.messages(end['session_id'])[-1]['content']=='Отменено пользователем'
    assert current.get() is None
    await agent.llm.http.aclose();store.close()


async def test_child_shares_parent_token_budget(tmp_path):
    agent, store, _ = make_agent(tmp_path, [], permissions={'mode':'allow_all'}, max_turn_tokens=65536)
    agent.registry.add(Subagent())
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'choices':[{'message':{'content':None,'tool_calls':[{'id':'child-budget','type':'function','function':{'name':'subagent','arguments':json.dumps({'task':'подзадача'})}}]},'finish_reason':'tool_calls'}],
                                        'usage':{'prompt_tokens':70000,'completion_tokens':1}})
    agent.llm=LLMClient(httpx.AsyncClient(base_url='http://fixture/v1',transport=httpx.MockTransport(handle)))
    events=await run(agent)
    assert len(calls)==1
    assert next(e for e in events if e['type']=='subagent_end')['reason']=='budget'
    assert events[-1]['reason']=='budget'
    await agent.llm.http.aclose();store.close()
