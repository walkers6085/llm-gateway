"""LLM-сводка, резервное сжатие, отмена и расход бюджета."""
import asyncio
import json
from contextlib import aclosing
import pytest

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


async def test_manual_summary_failure_and_no_budget_preserve_context(tmp_path):
    for name, script, budget, expected in (
        ('error', [{'status': 502, 'error': 'недоступно'}], 65536, 1),
        ('budget', [], 1, 0),
    ):
        folder = tmp_path / name
        seen = []
        agent, store, usage = make_agent(folder, script, seen, max_turn_tokens=budget,
                                         context_compaction={'mode': 'model'}, model_retry={'max_retries': 0})
        history(store, agent.session.id)
        assert not await agent.compact()
        assert len(seen) == expected
        assert store.get(agent.session.id).summary == ''
        assert store.get(agent.session.id).context_seq == 0
        assert agent.last_compaction['method'] == 'failed'
        assert agent.last_compaction['reason']
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


async def test_manual_summary_timeout_preserves_context_and_does_not_execute_tools(tmp_path):
    agent, store, _ = make_agent(tmp_path, [], context_compaction={'mode': 'model', 'timeout': .01})
    history(store, agent.session.id)
    async def waiting(*args, **kwargs):
        await asyncio.Event().wait()
        yield
    agent.llm.stream = waiting
    assert not await agent.compact()
    assert store.get(agent.session.id).summary == ''
    assert store.get(agent.session.id).context_seq == 0
    assert 'таймаут' in agent.last_compaction['reason']
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


async def test_manual_summary_includes_latest_completed_turn_even_with_local_auto_mode(tmp_path):
    seen = []
    agent, store, _ = make_agent(tmp_path, [
        {'content': 'Сводка обеих задач: проверить main.py и cli.py.'}, {'content': 'Продолжаю'}], seen)
    sid = agent.session.id
    history(store, sid)
    store.add_message(sid, {'role': 'user', 'content': 'Последняя задача: cli.py'})
    store.add_message(sid, {'role': 'assistant', 'content': 'Важный последний результат ' * 3000})
    before = store.messages(sid)
    size = agent.context_status()['estimated_tokens']
    assert await agent.compact()
    assert agent.last_compaction['method'] == 'model'
    assert 'Важный последний результат' in seen[0]['messages'][-1]['content']
    assert agent.context_status()['estimated_tokens'] < size / 5
    assert store.messages(sid) == before
    assert not await agent.compact()
    assert len(seen) == 1
    events = await run(agent, 'Проверь результат')
    assert events[-1]['reason'] == 'stop'
    assert 'Сводка обеих задач' in seen[-1]['messages'][0]['content']
    assert any(m.get('content') == 'Проверь результат' for m in seen[-1]['messages'])
    await agent.llm.http.aclose()
    store.close()


@pytest.mark.parametrize('status', [429, 502])
async def test_manual_summary_transient_failure_retries_model_only(tmp_path, status):
    seen = []
    agent, store, _ = make_agent(tmp_path, [
        {'status': status, 'error': 'попробуйте позже'}, {'content': 'Сводка после повтора'}], seen,
        model_retry={'initial_delay': 0, 'max_delay': 0})
    history(store, agent.session.id)
    before = store.messages(agent.session.id)
    progress = []
    def notify(event):
        assert len(seen) == 1, 'уведомление должно прийти до повторной попытки'
        progress.append(event)
    assert await agent.compact(on_progress=notify)
    assert progress[0]['retry'] == {'purpose': 'compaction', 'attempt': 1,
                                  'max_retries': 5, 'status': status, 'delay': 0}
    assert progress[0]['request_id']
    assert len(seen) == 2
    assert store.messages(agent.session.id) == before
    assert 'Сводка после повтора' in store.get(agent.session.id).summary
    retry = next(row for row in store.traces(agent.session.id) if row['event'] == 'model.retry')
    assert retry['data']['purpose'] == 'compaction'
    assert not store.tool_runs(agent.session.id)
    await agent.llm.http.aclose()
    store.close()


async def test_auto_summary_retry_is_visible_and_cancellable_before_next_attempt(tmp_path):
    seen = []
    agent, store, _ = make_agent(tmp_path, [{'status': 502, 'error': 'временный сбой'}], seen,
        context_compaction={'mode': 'model'}, max_input_tokens=9000,
        model_retry={'initial_delay': 60, 'max_delay': 60})
    history(store, agent.session.id, repeat=600)
    async with aclosing(agent.run('Продолжи')) as events:
        async for event in events:
            if event.get('retry'):
                assert event['retry']['purpose'] == 'compaction'
                assert event['retry']['status'] == 502
                break
        else:
            assert False, 'повтор должен быть виден до окончания ожидания'
    assert len(seen) == 1
    assert store.get(agent.session.id).context_seq == 0
    assert store.get(agent.session.id).summary == ''
    await agent.llm.http.aclose()
    store.close()


async def test_manual_summary_without_connection_does_not_trim_history(tmp_path):
    agent, store, _ = make_agent(tmp_path, [])
    history(store, agent.session.id)
    await agent.llm.http.aclose()
    agent.llm = None
    before = store.context_messages(agent.session.id)
    assert not await agent.compact()
    assert agent.last_compaction['method'] == 'failed'
    assert 'нет подключения' in agent.last_compaction['reason']
    assert store.context_messages(agent.session.id) == before
    assert store.get(agent.session.id).summary == ''
    store.close()


async def test_manual_summary_recovers_full_history_after_old_local_compaction(tmp_path):
    seen = []
    agent, store, _ = make_agent(tmp_path, [{'content': 'Восстановлена важная деталь: secret-file.py'}], seen)
    sid = agent.session.id
    store.add_message(sid, {'role': 'user', 'content': 'Проверить проект'})
    store.add_message(sid, {'role': 'assistant', 'content': 'Начало ' * 200 + ' Важная деталь: secret-file.py'})
    before = store.messages(sid)
    assert store.compact(sid, max_chars=80)
    assert 'secret-file.py' not in store.get(sid).summary
    assert await agent.compact()
    assert 'secret-file.py' in seen[0]['messages'][-1]['content']
    assert store.messages(sid) == before
    assert 'Восстановлена важная деталь' in store.get(sid).summary
    await agent.llm.http.aclose()
    store.close()
