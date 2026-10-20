"""Сжатие контекста без утраты запроса и повторного выполнения инструментов."""
import json

from agent.store import SessionStore
from tests.fakes import tool_call
from tests.test_loop import make_agent, run


def test_manual_compact_single_turn_and_resume(tmp_path):
    store = SessionStore(tmp_path / 'session.db')
    sid = store.create('local').id
    query = {'role': 'user', 'content': 'Цель: исправить поддержку Unicode без зависимостей'}
    store.add_message(sid, query)
    store.add_message(sid, {'role': 'assistant', 'content': 'Результат ' * 2000})
    original = store.messages(sid)
    assert store.compact(sid)
    assert store.messages(sid) == original
    assert store.context_messages(sid) == [query]
    assert len(store.get(sid).summary) < 4100
    assert not store.compact(sid)
    store.close()
    store = SessionStore(tmp_path / 'session.db')
    assert store.context_messages(sid) == [query]
    store.close()


async def test_auto_compact_after_large_tool_preserves_pair_and_request(tmp_path):
    seen = []
    call = tool_call('first', 'echo', text='кратко')
    agent, store, usage = make_agent(tmp_path, [{'tool_calls': [call]}, {'content': 'готово'}], seen,
                                     max_input_tokens=7500, max_turn_tokens=100000)
    echo = agent.registry.get('echo')
    async def large(args, ctx):
        return 'Данные ' * 4000
    echo.run = large
    events = await run(agent, 'Последний запрос: исправь задачу, сохрани Unicode')
    assert events[-1]['reason'] == 'stop'
    assert any(e['type'] == 'warning' and 'сокращён' in e['message'] for e in events)
    assert len(seen) == 2
    messages = seen[-1]['messages']
    assert any(m.get('content') == 'Последний запрос: исправь задачу, сохрани Unicode' for m in messages)
    assert [m['tool_call_id'] for m in messages if m['role'] == 'tool'] == ['first']
    assert len([e for e in events if e['type'] == 'tool_result']) == 1
    assert len(next(m['content'] for m in store.messages(agent.session.id) if m['role'] == 'tool')) > 20000
    await agent.llm.http.aclose()
    store.close()


async def test_provider_overflow_retries_once_and_pins_goal_and_latest(tmp_path):
    seen = []
    script = [{'status': 400, 'error': 'maximum context length exceeded'}, {'content': 'готово'}]
    agent, store, usage = make_agent(tmp_path, script, seen, max_turn_tokens=200000, max_input_tokens=100000)
    sid = agent.session.id
    store.add_message(sid, {'role': 'user', 'content': 'Цель: собрать CLI без новых зависимостей'})
    store.add_message(sid, {'role': 'assistant', 'content': 'старый ответ ' * 1600})
    events = await run(agent, 'Последний запрос: исправить /compact')
    assert events[-1]['reason'] == 'stop'
    assert len(seen) == 2
    assert len(json.dumps(seen[1]).encode()) < len(json.dumps(seen[0]).encode())
    text = json.dumps(seen[1]['messages'], ensure_ascii=False)
    assert 'Цель: собрать CLI без новых зависимостей' in text
    assert any(m['role'] == 'user' and (m.get('content') or '').startswith('Исходная цель сессии:') for m in seen[1]['messages'])
    assert 'Последний запрос: исправить /compact' in text
    assert len(store.messages(sid)) == 4
    await agent.llm.http.aclose()
    store.close()


async def test_overflow_does_not_loop_or_retry_other_errors(tmp_path):
    for status, error in [(400, 'maximum context length exceeded'), (401, 'нет доступа')]:
        folder = tmp_path / str(status)
        folder.mkdir()
        seen = []
        agent, store, usage = make_agent(folder, [{'status': status, 'error': error}] * 3, seen, max_input_tokens=100000)
        store.add_message(agent.session.id, {'role': 'user', 'content': 'прошлая задача'})
        store.add_message(agent.session.id, {'role': 'assistant', 'content': 'старый ответ ' * 1600})
        events = await run(agent, 'Продолжи')
        assert events[-1]['reason'] == 'error'
        assert len(seen) == (2 if status == 400 else 1)
        await agent.llm.http.aclose()
        store.close()


async def test_turn_budget_is_not_reset_by_compaction(tmp_path):
    agent, store, usage = make_agent(tmp_path, [{'content': 'не должен вызываться'}], max_turn_tokens=1)
    events = await run(agent)
    assert events[-1]['reason'] == 'budget'
    assert 'max_turn_tokens' in events[-2]['message']
    assert not usage
    await agent.llm.http.aclose()
    store.close()


async def test_overflow_mid_loop_retries_model_without_reexecuting_tool(tmp_path):
    seen = []
    agent, store, usage = make_agent(tmp_path, [
        {'tool_calls': [tool_call('once', 'echo', text='кратко')]},
        {'status': 400, 'error': 'context_length_exceeded'},
        {'content': 'продолжение'},
    ], seen, max_input_tokens=100000, max_turn_tokens=200000)
    count = 0
    async def large(args, ctx):
        nonlocal count
        count += 1
        return 'Данные ' * 4000
    agent.registry.get('echo').run = large
    events = await run(agent, 'Сохрани цель и продолжай после переполнения')
    assert events[-1]['reason'] == 'stop'
    assert len(seen) == 3 and count == 1
    assert next(m for m in seen[-1]['messages'] if m['role'] == 'tool')['tool_call_id'] == 'once'
    assert any(m.get('content') == 'Сохрани цель и продолжай после переполнения' for m in seen[-1]['messages'])
    await agent.llm.http.aclose()
    store.close()


async def test_compaction_keeps_actual_user_request_after_stop_hook_message(tmp_path):
    agent, store, usage = make_agent(tmp_path, [])
    sid = agent.session.id
    query = {'role': 'user', 'content': 'Настоящий запрос пользователя'}
    store.add_message(sid, query, {'user_input': query['content']})
    store.add_message(sid, {'role': 'assistant', 'content': 'Ответ ' * 2000})
    store.add_message(sid, {'role': 'user', 'content': 'Продолжи проверку из хука'})
    messages, estimate, changed = agent._context(agent.registry.specs(), 7500)
    assert changed
    assert query in messages
    assert any(m.get('content') == 'Продолжи проверку из хука' for m in messages)
    await agent.llm.http.aclose()
    store.close()


async def test_partial_stream_context_error_is_not_retried(tmp_path):
    from agent.llm import LLMError
    agent, store, usage = make_agent(tmp_path, [])
    calls = 0
    async def partial(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield {'type': 'text', 'text': 'Частичный ответ'}
        raise LLMError(502, 'context_length_exceeded')
    agent.llm.stream = partial
    events = await run(agent)
    assert calls == 1
    assert events[-1]['reason'] == 'error'
    assert not any(message['role'] == 'assistant' for message in store.messages(agent.session.id))
    await agent.llm.http.aclose()
    store.close()
