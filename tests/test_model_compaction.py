"""LLM-сводка, резервное сжатие, отмена и расход бюджета."""
import asyncio
import json

from agent.llm import LLMError
from agent.compaction import wire_size
from tests.test_loop import make_agent, run


def history(store, sid, repeat=100):
    store.add_message(sid, {'role': 'user', 'content': 'Собрать CLI без новых зависимостей'})
    store.add_message(sid, {'role': 'assistant', 'content': 'Изменён файл main.py, остаётся проверить тесты. ' * repeat})


async def test_manual_model_summary_preserves_history_and_traces(tmp_path):
    seen = []
    agent, store, usage = make_agent(tmp_path, [{'content': 'Цель: CLI. Изменён main.py. Осталось проверить тесты.'}], seen,
                                     context_compaction={'mode': 'model'})
    history(store, agent.session.id)
    before = store.messages(agent.session.id)
    assert await agent.compact()
    assert store.messages(agent.session.id) == before
    assert 'Изменён main.py' in store.get(agent.session.id).summary
    assert len(seen) == 1 and 'tools' not in seen[0]
    assert usage == [('alpha', 200, (7, 3))]
    traces = store.traces(agent.session.id)
    start = next(row for row in traces if row['event'] == 'model.start')
    assert start['data']['purpose'] == 'compaction'
    assert start['request_id']
    await agent.llm.http.aclose()
    store.close()


async def test_summary_failure_and_no_budget_fall_back(tmp_path):
    for name, script, budget, expected in (
        ('error', [{'status': 502, 'error': 'недоступно'}], 65536, 1),
        ('budget', [], 1, 0),
    ):
        folder = tmp_path / name
        seen = []
        agent, store, usage = make_agent(folder, script, seen, max_turn_tokens=budget,
                                         context_compaction={'mode': 'model'})
        history(store, agent.session.id)
        assert await agent.compact()
        assert len(seen) == expected
        assert store.get(agent.session.id).summary.startswith('Сокращённые выдержки')
        await agent.llm.http.aclose()
        store.close()


async def test_cancel_summary_preserves_checkpoint(tmp_path):
    agent, store, _ = make_agent(tmp_path, [], context_compaction={'mode': 'model'})
    history(store, agent.session.id)
    started = asyncio.Event()
    async def waiting(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
        yield
    agent.llm.stream = waiting
    task = asyncio.create_task(agent.compact())
    await started.wait()
    task.cancel()
    try:
        await task
        assert False, 'отмена должна выйти наружу'
    except asyncio.CancelledError:
        pass
    session = store.get(agent.session.id)
    assert session.summary == '' and session.context_seq == 0
    await agent.llm.http.aclose()
    store.close()


async def test_chunked_summary_and_auto_continue(tmp_path):
    seen = []
    agent, store, usage = make_agent(tmp_path, [], seen, max_input_tokens=9000, max_turn_tokens=100000,
                                     context_compaction={'mode': 'model'})
    count = 0
    async def reply(model, messages, tools=None, temperature=None):
        nonlocal count
        seen.append({'messages': messages, 'tools': tools})
        count += 1
        summary = 'Составь краткую рабочую сводку' in messages[0]['content']
        text = 'Сводка: main.py, проверить тесты.' if summary else 'Готово'
        yield {'type': 'message', 'message': {'role': 'assistant', 'content': text}, 'usage': (7, 3)}
    agent.llm.stream = reply
    history(store, agent.session.id, repeat=600)
    events = await run(agent, 'Продолжи проверку')
    assert events[-1]['reason'] == 'stop'
    assert count >= 3
    summaries = seen[:-1]
    assert all(not item['tools'] for item in summaries)
    assert all(wire_size(item['messages']) <= 9000 for item in summaries)
    assert any(m.get('content') == 'Продолжи проверку' for m in seen[-1]['messages'])
    assert 'Сводка:' in store.get(agent.session.id).summary
    assert len(store.messages(agent.session.id)) == 4
    await agent.llm.http.aclose()
    store.close()


def test_compaction_rejects_stale_history(tmp_path):
    from agent.store import SessionStore
    store = SessionStore(tmp_path / 's.db')
    sid = store.create('local').id
    history(store, sid)
    candidate = store.compaction_candidate(sid)
    store.add_message(sid, {'role': 'user', 'content': 'Новое сообщение'})
    try:
        store.commit_compaction(sid, candidate, 'Устаревшая сводка')
        assert False, 'нельзя применить устаревший результат'
    except ValueError:
        pass
    assert store.get(sid).context_seq == 0
    store.close()


async def test_summary_timeout_falls_back_and_does_not_execute_tools(tmp_path):
    agent, store, _ = make_agent(tmp_path, [], context_compaction={'mode': 'model', 'timeout': .01})
    history(store, agent.session.id)
    async def waiting(*args, **kwargs):
        await asyncio.Event().wait()
        yield
    agent.llm.stream = waiting
    assert await agent.compact()
    assert store.get(agent.session.id).summary.startswith('Сокращённые выдержки')
    assert not store.tool_runs(agent.session.id)
    await agent.llm.http.aclose()
    store.close()


async def test_summary_input_uses_cleanup_and_keeps_original_tool_output(tmp_path):
    from tests.fakes import tool_call
    seen = []
    agent, store, _ = make_agent(tmp_path, [{'content': 'Сводка результата'}], seen, context_compaction={'mode': 'model'})
    sid = agent.session.id
    store.add_message(sid, {'role': 'user', 'content': 'Проверка'})
    store.add_message(sid, {'role': 'assistant', 'tool_calls': [tool_call('result', 'echo')]})
    raw = '\x1b[31mДанные\x1b[0m\n\n\nИтог'
    store.add_message(sid, {'role': 'tool', 'tool_call_id': 'result', 'content': raw})
    store.add_message(sid, {'role': 'assistant', 'content': 'Готово'})
    assert await agent.compact()
    sent = json.dumps(seen, ensure_ascii=False)
    assert 'u001b' not in sent and '31m' not in sent
    assert store.messages(sid)[2]['content'] == raw
    await agent.llm.http.aclose()
    store.close()
