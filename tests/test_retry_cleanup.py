"""Backoff и очистка запросов: целостность истории, точность JSON и отмена ожидания."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json

import httpx
import pytest

from agent.context import clean_messages, clean_text
from agent.llm import LLMError
from agent.mcp import render_result
from agent.retry import delay_for, retry_after
from agent.settings import DEFAULTS
from tests.fakes import tool_call
from tests.test_loop import make_agent, run

CLEAN = DEFAULTS['context_cleanup']
FAST = {'max_retries':3,'initial_delay':0,'max_delay':0,'max_elapsed':60}


def test_backoff_is_bounded_and_honors_retry_after(monkeypatch):
    monkeypatch.setattr('agent.retry.random.uniform',lambda *_:0)
    config={'max_retries':5,'initial_delay':2,'max_delay':10,'max_elapsed':180}
    error=LLMError(429,'лимит',retryable=True)
    assert [delay_for(error,i,0,config) for i in range(6)]==[2,4,8,10,10,None]
    error.retry_after=30
    assert delay_for(error,0,0,config)==30
    assert delay_for(error,0,160,config) is None
    assert delay_for(LLMError(401,'доступ'),0,0,config) is None
    assert retry_after('3')==3 and retry_after('-1')==0
    assert retry_after('nan') is None and retry_after('bad') is None
    remaining=retry_after(format_datetime(datetime.now(timezone.utc)+timedelta(seconds=30)))
    assert 28<remaining<=30


async def test_429_recovers_in_same_turn_and_tools_are_not_repeated(tmp_path):
    seen=[]
    script=[{'tool_calls':[tool_call('c','echo',text='раз')]}]+[{'status':429,'error':'лимит'}]*2+[{'content':'Готово'}]
    agent,store,usage=make_agent(tmp_path,script,seen,model_retry=FAST)
    try:
        semaphore=asyncio.Semaphore(1);agent.llm.limiter=lambda _:semaphore
        events=[]
        async for event in agent.run('задача'):
            events.append(event)
            if event.get('retry'):assert not semaphore.locked()
        assert events[-1]['reason']=='stop'
        assert len([m for m in store.messages(agent.session.id) if m['role']=='user'])==1
        assert len([m for m in store.messages(agent.session.id) if m['role']=='tool'])==1
        assert len([e for e in events if e['type']=='tool_call'])==1
        retries=[e for e in events if e.get('retry')]
        assert [e['retry']['attempt'] for e in retries]==[1,2]
        assert len({e['request_id'] for e in events})==1
        logs=store.traces(agent.session.id)
        attempts=[row for row in logs if row['event']=='model.start']
        assert len(attempts)==4 and len({row['span_id'] for row in attempts})==4
        assert len([row for row in logs if row['event']=='model.retry'])==2
        assert [entry[1] for entry in usage]==[200,429,429,200]
    finally:
        await agent.llm.http.aclose();store.close()


async def test_retry_exhaustion_keeps_session_available(tmp_path):
    agent,store,_=make_agent(tmp_path,[{'status':429,'error':'лимит'}]*4+[{'content':'Продолжение'}],model_retry=FAST)
    try:
        assert (await run(agent))[-1]['reason']=='error'
        assert (await run(agent,'Ещё запрос'))[-1]['reason']=='stop'
        assert len([m for m in store.messages(agent.session.id) if m['role']=='user'])==2
    finally:
        await agent.llm.http.aclose();store.close()


async def test_retry_after_too_long_is_not_shortened(tmp_path):
    seen=[]
    agent,store,_=make_agent(tmp_path,[],model_retry=FAST)
    def handler(request):
        seen.append(request)
        return httpx.Response(429,headers={'Retry-After':'3600'},json={'error':{'message':'лимит'}})
    await agent.llm.http.aclose()
    agent.llm.http=httpx.AsyncClient(base_url='https://example.test/v1',transport=httpx.MockTransport(handler))
    try:
        assert (await run(agent))[-1]['reason']=='error' and len(seen)==1
    finally:
        await agent.llm.http.aclose();store.close()


async def test_cancel_during_backoff_is_immediate_and_history_valid(tmp_path):
    agent,store,_=make_agent(tmp_path,[{'tool_calls':[tool_call('c','echo',text='раз')]},{'status':429,'error':'лимит'},{'content':'Продолжение'}],
                                model_retry={'initial_delay':60,'max_delay':60,'max_elapsed':180})
    waiting=asyncio.Event()
    async def consume():
        async for event in agent.run('Работай'):
            if event.get('retry'):waiting.set()
    task=asyncio.create_task(consume())
    try:
        await asyncio.wait_for(waiting.wait(),2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,1)
        messages=store.messages(agent.session.id)
        assert [m['tool_call_id'] for m in messages if m['role']=='tool']==['c']
        assert (await run(agent,'Продолжи'))[-1]['reason']=='stop'
    finally:
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        await agent.llm.http.aclose();store.close()


async def test_streamed_content_is_not_retried(tmp_path):
    agent,store,_=make_agent(tmp_path,[],model_retry=FAST);seen=[]
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"start"}}]}\n\n'
            raise httpx.ReadError('обрыв')
    def handler(request):
        seen.append(request)
        return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Broken())
    await agent.llm.http.aclose();agent.llm.http=httpx.AsyncClient(base_url='https://example.test/v1',transport=httpx.MockTransport(handler))
    try:
        events=await run(agent)
        assert events[-1]['reason']=='error' and len(seen)==1
        assert not any(e.get('retry') for e in events)
    finally:
        await agent.llm.http.aclose();store.close()


async def test_steering_during_backoff_is_used_before_retry(tmp_path):
    seen=[];agent,store,_=make_agent(tmp_path,[{'status':429,'error':'лимит'},{'content':'Готово'}],seen,model_retry=FAST)
    try:
        events=[]
        async for event in agent.run('Исходный запрос'):
            events.append(event)
            if event.get('retry'):agent.steer('Уточнение во время ожидания',input_id='steer-retry')
        assert events[-1]['reason']=='stop'
        assert any(m.get('content')=='Уточнение во время ожидания' for m in seen[1]['messages'])
    finally:
        await agent.llm.http.aclose();store.close()


def test_cleaning_is_lossless_for_protocol_code_numbers_and_unicode():
    source=[{'role':'user','content':'```python\nx = "\\x1b"\n```'},
            {'role':'assistant','content':None,'tool_calls':[{'id':'a','type':'function','function':{'name':'read_file','arguments':'{}'}},{'id':'b','type':'function','function':{'name':'mcp__s__json','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'a','content':'  first\n\n\n    second\n'},
            {'role':'tool','tool_call_id':'b','content':'\x1b[31m{ "n": 1.234567890123456789, "s": " a  b ", "a": [1, 1], "u": "👩‍💻" }\x1b[0m'}]
    original=copy.deepcopy(source);cleaned,stats=clean_messages(source,CLEAN)
    assert source==original and cleaned[:3]==source[:3]
    assert cleaned[3]['tool_call_id']=='b'
    assert cleaned[3]['content']=='{"n":1.234567890123456789,"s":" a  b ","a":[1,1],"u":"👩‍💻"}'
    assert stats['changed_tools']==1 and stats['before_bytes']>stats['after_bytes']
    assert clean_text('a\x1b]52;c;payload\x07b\x00',CLEAN)=='ab'
    assert clean_text('a\x1b]52;c;unfinished',CLEAN)=='a'
    assert clean_text('a\x9b31mb\x9b0m',CLEAN)=='ab'
    assert clean_text('x\n\n\n\ny',CLEAN)=='x\n\ny'
    assert clean_text('```\nx\n\n\n\ny\n```',CLEAN)=='```\nx\n\n\n\ny\n```'
    repeated='log\n'*5
    assert clean_text(repeated,CLEAN)==repeated
    assert '5 раз' in clean_text(repeated,{**CLEAN,'deduplicate_lines':True})
    assert clean_messages(source,{**CLEAN,'enabled':False})[0]==source


async def test_only_request_is_cleaned_not_saved_tool_result(tmp_path):
    text='\x1b[31mважно\x1b[0m\n\n\n\nхвост';seen=[]
    agent,store,_=make_agent(tmp_path,[{'tool_calls':[tool_call('t','echo',text=text)]},{'content':'Готово'}],seen)
    try:
        assert (await run(agent))[-1]['reason']=='stop'
        saved=next(m['content'] for m in store.messages(agent.session.id) if m['role']=='tool')
        wire=next(m['content'] for m in seen[1]['messages'] if m['role']=='tool')
        assert '\x1b[31m' in saved and '\x1b' not in wire and '\n\n\n' not in wire
        assert any(row['event']=='context.cleaned' for row in store.traces(agent.session.id))
    finally:
        await agent.llm.http.aclose();store.close()


def test_mcp_structured_content_is_preserved_without_duplicate_wrapper():
    output=render_result({'content':[{'type':'text','text':'Краткий итог'}],'structuredContent':{'count':3}})
    assert 'Краткий итог' in output.content and '"count": 3' in output.content
    output=render_result({'content':[{'type':'text','text':'{ "count": 3 }'}],'structuredContent':{'count':3}})
    assert output.content=='{ "count": 3 }'


async def test_temporary_errors_retry_but_permanent_and_invalid_json_do_not(tmp_path):
    for status in (400,401,403,408,429,500,502,503,504):
        seen=[];folder=tmp_path/str(status);folder.mkdir()
        agent,store,_=make_agent(folder,[{'status':status,'error':'проверка'},{'content':'Готово'}],seen,model_retry=FAST)
        try:
            events=await run(agent)
            transient=status in (408,429,500,502,503,504)
            assert len(seen)==(2 if transient else 1)
            assert events[-1]['reason']==('stop' if transient else 'error')
        finally:
            await agent.llm.http.aclose();store.close()
    agent,store,_=make_agent(tmp_path/'invalid',[],model_retry=FAST);seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(200,content=b'invalid-json',headers={'content-type':'application/json'})
    await agent.llm.http.aclose();agent.llm.http=httpx.AsyncClient(base_url='https://example.test/v1',transport=httpx.MockTransport(handler))
    try:
        assert (await run(agent))[-1]['reason']=='error' and len(seen)==1
    finally:
        await agent.llm.http.aclose();store.close()


async def test_structured_error_message_and_sse_429_are_recoverable(tmp_path):
    for streaming in (False,True):
        agent,store,_=make_agent(tmp_path/str(streaming),[],model_retry=FAST);seen=[]
        def handler(request):
            seen.append(request)
            if len(seen)==1:
                if streaming:
                    return httpx.Response(200,content=b'data: {"error":{"code":429,"message":{"detail":"limit"}}}\n\n',headers={'content-type':'text/event-stream'})
                return httpx.Response(429,json={'error':{'message':{'detail':'limit'}}})
            return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}]})
        await agent.llm.http.aclose();agent.llm.http=httpx.AsyncClient(base_url='https://example.test/v1',transport=httpx.MockTransport(handler))
        try:
            assert (await run(agent))[-1]['reason']=='stop' and len(seen)==2
        finally:
            await agent.llm.http.aclose();store.close()
