"""Уточнения внутри текущего цикла: поток, инструменты, отмена, история и снимки."""
import asyncio
import json

import pytest

from agent.loop import Agent
from agent.store import SessionStore
from agent.tools.files import WriteFile
from agent.hooks import HookResult
from tests.fakes import tool_call
from tests.test_loop import make_agent, run


async def test_steer_during_stream_uses_same_cycle(tmp_path):
    agent, store, _ = make_agent(tmp_path, [])
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def stream(model, messages, *args):
        seen.append(messages)
        if len(seen) == 1:
            yield {'type': 'text', 'text': 'Старый ответ'}
            entered.set(); await release.wait()
        yield {'type': 'message', 'message': {'role': 'assistant', 'content': 'Ответ'}, 'usage': (10, 5)}
    agent.llm.stream = stream
    task = asyncio.create_task(run(agent, 'Исходный запрос'))
    await entered.wait()
    item = agent.steer('Учти Unicode', input_id='input-one')
    assert store.pending_inputs(agent.session.id)[0]['id'] == item['id']
    release.set(); events = await task
    assert len(seen) == 2 and sum(e['type'] == 'done' for e in events) == 1
    assert any(m.get('content') == 'Учти Unicode' for m in seen[-1])
    assert len({e['request_id'] for e in events}) == 1
    assert not store.pending_inputs(agent.session.id)
    models = [row for row in store.traces(agent.session.id) if row['event'] == 'model.start']
    assert models[-1]['data']['input_id'] == 'input-one'
    await agent.llm.http.aclose(); store.close()


async def test_steer_finishes_running_tool_and_skips_rest_of_batch(tmp_path):
    seen = []
    agent, store, _ = make_agent(tmp_path, [{'tool_calls': [tool_call('first', 'echo', text='one'), tool_call('skip', 'echo', text='two')]}, {'content': 'Учтено'}], seen)
    entered, release = asyncio.Event(), asyncio.Event(); executed = []
    async def slow(args, ctx):
        executed.append(args['text']); entered.set(); await release.wait(); return 'Завершено'
    agent.registry.get('echo').run = slow
    task = asyncio.create_task(run(agent)); await entered.wait()
    agent.steer('Не выполняй вторую команду'); release.set(); events = await task
    assert executed == ['one']
    assert next(e for e in events if e['type'] == 'tool_result' and e['id'] == 'skip')['decision'] == 'steering:skip'
    messages = seen[-1]['messages']
    roles = [m['role'] for m in messages]
    assert roles[-4:] == ['assistant', 'tool', 'tool', 'user']
    assert messages[-1]['content'] == 'Не выполняй вторую команду'
    assert {m.get('tool_call_id') for m in messages if m['role'] == 'tool'} == {'first', 'skip'}
    await agent.llm.http.aclose(); store.close()


async def test_cancelled_input_survives_reopen_and_newer_request_remains_last(tmp_path):
    seen = []
    agent, store, _ = make_agent(tmp_path, [{'tool_calls': [tool_call('slow', 'slow')]}, {'content': 'Продолжаю'}], seen)
    events = []
    async def consume():
        async for event in agent.run('Начальная задача'):
            events.append(event)
    task = asyncio.create_task(consume())
    for _ in range(100):
        if any(e['type'] == 'tool_call' for e in events):
            break
        await asyncio.sleep(.01)
    agent.steer('Старая правка', input_id='persisted'); task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    sid = agent.session.id; store.close(); reopened = SessionStore(tmp_path / 's.db')
    assert reopened.pending_inputs(sid)[0]['id'] == 'persisted'
    resumed = Agent(llm=agent.llm, registry=agent.registry, settings=agent.settings, store=reopened, session=reopened.get(sid))
    result = await run(resumed, 'Новая правка важнее')
    assert result[-1]['reason'] == 'stop'
    users = [m['content'] for m in seen[-1]['messages'] if m['role'] == 'user']
    assert users[-2:] == ['Старая правка', 'Новая правка важнее']
    assert not reopened.pending_inputs(sid)
    recovered_row = next(row for row in reopened.timeline(sid) if row['metadata'].get('steering'))
    assert reopened.revert_checkpoints(sid, recovered_row['seq'])
    await agent.llm.http.aclose(); reopened.close()


async def test_hook_rejects_queued_input(tmp_path):
    agent, store, _ = make_agent(tmp_path, [])
    async def hook(event, payload, tool_name=None):
        return HookResult(decision='deny', reason='запрет') if payload.get('steering') else HookResult()
    agent.hooks.run = hook
    count = 0
    async def stream(*args):
        nonlocal count
        count += 1
        agent.steer('Запрещённое уточнение')
        yield {'type': 'message', 'message': {'role': 'assistant', 'content': 'Готово'}, 'usage': (10, 5)}
    agent.llm.stream = stream
    events = await run(agent)
    assert count == 1 and any(e['type'] == 'input_rejected' for e in events)
    assert not any(m.get('content') == 'Запрещённое уточнение' for m in store.messages(agent.session.id))
    assert not store.pending_inputs(agent.session.id)
    await agent.llm.http.aclose(); store.close()


async def test_revert_steering_restores_only_changes_after_injection(tmp_path):
    agent, store, _ = make_agent(tmp_path, [
        {'tool_calls': [tool_call('before', 'write_file', path='before.txt', content='before'), tool_call('pause', 'echo', text='pause')]},
        {'tool_calls': [tool_call('after', 'write_file', path='after.txt', content='after')]}, {'content': 'Готово'}],
        permissions={'mode': 'allow_all'})
    agent.registry.add(WriteFile())
    async def pause(args, ctx):
        agent.steer('Теперь добавь другой файл', input_id='checkpoint-input'); return 'Пауза завершена'
    agent.registry.get('echo').run = pause
    events = await run(agent)
    assert events[-1]['reason'] == 'stop' and (tmp_path / 'before.txt').exists() and (tmp_path / 'after.txt').exists()
    row = next(row for row in store.timeline(agent.session.id) if row['metadata'].get('steering'))
    store.rewind(agent.session.id, row['seq'], revision=store.revision(agent.session.id), files=True)
    assert (tmp_path / 'before.txt').exists() and not (tmp_path / 'after.txt').exists()
    await agent.llm.http.aclose(); store.close()


def test_queue_bounded_idempotent_and_revert_clears_pending(tmp_path):
    store = SessionStore(tmp_path / 's.db'); sid = store.create('local').id
    store.enqueue_input(sid, 'request', 'one', 'text')
    store.enqueue_input(sid, 'request', 'one', 'text')
    assert len(store.pending_inputs(sid)) == 1
    for i in range(31):
        store.enqueue_input(sid, 'request', str(i), 'text')
    with pytest.raises(ValueError, match='заполнена'):
        store.enqueue_input(sid, 'request', 'overflow', 'text')
    seq = store.add_message(sid, {'role': 'user', 'content': 'Запрос'})
    store.rewind(sid, seq, revision=store.revision(sid))
    assert not store.pending_inputs(sid)
    store.close()
