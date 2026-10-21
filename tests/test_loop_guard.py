"""Точные циклы, продление блоков и сохранность истории без настоящих моделей."""

import json

import pytest

from agent.loop_guard import DEFAULTS, LoopGuard
from agent.settings import DEFAULTS as SETTINGS, SettingsError, check, load, load_profiles, merge
from agent.tools.subagent import Subagent
from tests.fakes import tool_call
from tests.test_loop import make_agent, run, hook_cmd


def response(index, text='x', extra=False):
    calls = [tool_call(f'c{index}', 'echo', text=text)]
    if extra:
        calls.append(tool_call(f'skip{index}', 'danger', target='не выполнять'))
    return {'tool_calls': calls}


def assert_paired(messages):
    expected = [c['id'] for m in messages for c in m.get('tool_calls', [])]
    actual = [m['tool_call_id'] for m in messages if m['role'] == 'tool']
    assert sorted(expected) == sorted(actual)


@pytest.mark.parametrize('size', [1, 2, 3, 4])
def test_patterns_and_single_correction(size):
    guard = LoopGuard()
    for phase, action in enumerate(['corrected', 'stopped']):
        for i in range(size * 3):
            result = guard.observe('echo', {'text': i % size}, False, str(i % size))
            if i < size * 3 - 1:
                assert result is None
        assert result == {'action': action, 'pattern_length': size, 'repetitions': 3}
    guard.reset()
    assert not guard.corrected and not guard.history and guard.action is None
    assert DEFAULTS == SETTINGS['loop_guard']


def test_canonical_args_changed_results_and_window():
    guard = LoopGuard()
    assert guard.observe('x', {'b': [1, 2], 'a': 1}, False, 'same') is None
    assert guard.observe('x', {'a': 1, 'b': [1, 2]}, False, 'same') is None
    assert guard.observe('x', {'b': [1, 2], 'a': 1}, False, 'same')['action'] == 'corrected'
    for i in range(40):
        assert guard.observe('x', {'b': [1, 2]}, False, str(i)) is None
    assert len(guard.history) == 24
    for args in ({'b': [2, 1]}, {'b': [1, 2]}, {'b': [2, 1]}):
        assert guard.observe('x', args, False, 'same') is None
    disabled = LoopGuard({'enabled': False})
    assert all(disabled.observe('x', {}, False, '') is None for _ in range(50))


def test_guard_settings_validate_merged_layers(tmp_path):
    with pytest.raises(SettingsError, match='loop_guard.window'):
        check(merge(SETTINGS, {'loop_guard': {'window': 5}}))
    for key in ['window', 'repetitions', 'max_pattern_length']:
        with pytest.raises(SettingsError):
            check({'loop_guard': {key: 0}})
    user = tmp_path / 'settings.json'
    user.write_text(json.dumps({'loop_guard': {'window': 5}}))
    with pytest.raises(SettingsError, match='loop_guard.window'):
        load(user_file=user)
    user.write_text(json.dumps({'profiles': {'default': {'loop_guard': {'max_pattern_length': 9}}}}))
    with pytest.raises(SettingsError, match='loop_guard.window'):
        load_profiles(user)


async def test_correction_skips_batch_then_model_can_finish(tmp_path):
    seen = []
    agent, store, _ = make_agent(tmp_path, [response(0), response(1), response(2, extra=True), {'content': 'Другой подход'}], seen)
    try:
        events = await run(agent)
        assert events[-1]['reason'] == 'stop'
        assert next(e for e in events if e.get('id') == 'skip2')['decision'] == 'loop_guard:skip'
        assert not any(e['type'] == 'permission_request' for e in events)
        assert '<loop_guard>' in seen[-1]['messages'][-2]['content']
        assert_paired(store.messages(agent.session.id))
        traces = store.traces(agent.session.id)
        assert len([t for t in traces if t['event'] == 'loop_guard.corrected']) == 1
        assert not any('не выполнять' in json.dumps(t['data'], ensure_ascii=False) for t in traces if t['event'].startswith('loop_guard.'))
    finally:
        await agent.llm.http.aclose(); store.close()


async def test_second_cycle_stops_and_next_request_is_available(tmp_path):
    agent, store, _ = make_agent(tmp_path, [response(i, extra=i in (2, 5)) for i in range(6)] + [{'content': 'Продолжение'}], max_steps=2)
    try:
        events = await run(agent)
        assert events[-1]['reason'] == 'loop_detected'
        assert len([e for e in events if e['type'] == 'step']) == 6
        assert len([e for e in events if e.get('decision') == 'loop_guard:skip']) == 2
        assert_paired(store.messages(agent.session.id))
        traces = store.traces(agent.session.id)
        assert len([t for t in traces if t['event'] == 'loop_guard.stopped']) == 1
        assert all(t['request_id'] == events[-1]['request_id'] for t in traces)
        assert (await run(agent, 'Смени задачу'))[-1]['reason'] == 'stop'
    finally:
        await agent.llm.http.aclose(); store.close()


async def test_blocks_reset_only_local_counter(tmp_path):
    agent, store, _ = make_agent(tmp_path, [response(i, str(i)) for i in range(51)] + [{'content': 'Готово'}])
    try:
        events = await run(agent)
        steps = [e for e in events if e['type'] == 'step']
        assert events[-1]['reason'] == 'stop' and len(steps) == 52
        assert [e['total'] for e in steps] == list(range(1, 53))
        assert [(e['current'], e['total']) for e in steps if e['extended']] == [(1, 26), (1, 51)]
        assert len({e['request_id'] for e in steps}) == 1
        assert_paired(store.messages(agent.session.id))
    finally:
        await agent.llm.http.aclose(); store.close()


async def test_token_budget_is_not_reset_by_block_extension(tmp_path):
    agent, store, _ = make_agent(tmp_path, [response(i, str(i)) for i in range(40)], max_steps=1, model_retry={'max_retries': 0})
    try:
        from agent.model_limits import estimate_tokens
        # Вход вместе с расходом первых шагов помещается, на более позднем шаге — уже нет.
        base = estimate_tokens(agent._context_messages(120000), agent.tool_specs())
        agent.settings['max_turn_tokens'] = base + 500
        events = await run(agent)
        assert events[-1]['reason'] == 'budget'
        assert any(e['type'] == 'step' and e['extended'] for e in events)
    finally:
        await agent.llm.http.aclose(); store.close()


async def test_steering_resets_detector_but_not_steps(tmp_path):
    agent, store, _ = make_agent(tmp_path, [response(i) for i in range(6)] + [{'content': 'Готово'}], max_steps=2)
    try:
        events = []
        async for event in agent.run('Задача'):
            events.append(event)
            if event['type'] == 'tool_result' and event['id'] == 'c2':
                agent.steer('Повтори проверку ещё раз')
        assert events[-1]['reason'] == 'stop'
        assert len([e for e in events if e['type'] == 'warning' and e.get('loop_guard')]) == 2
        assert [e['total'] for e in events if e['type'] == 'step'] == list(range(1, 8))
        assert_paired(store.messages(agent.session.id))
    finally:
        await agent.llm.http.aclose(); store.close()


async def test_retries_do_not_count_as_new_steps(tmp_path):
    agent, store, _ = make_agent(tmp_path, [{'status': 502, 'error': 'обрыв'}, response(0), {'content': 'Готово'}],
                                  max_steps=1, model_retry={'initial_delay': 0, 'max_delay': 0})
    try:
        events = await run(agent)
        assert [e['total'] for e in events if e['type'] == 'step'] == [1, 2]
        assert events[-1]['reason'] == 'stop'
    finally:
        await agent.llm.http.aclose(); store.close()


async def test_compaction_does_not_reset_detector(tmp_path):
    agent, store, _ = make_agent(tmp_path, [response(i) for i in range(4)] + [{'content': 'Сводка задачи'}] + [response(4), response(5)])
    try:
        events = []
        async for event in agent.run('Задача'):
            events.append(event)
            if event['type'] == 'tool_result' and event['id'] == 'c3':
                assert await agent.compact()
        assert events[-1]['reason'] == 'loop_detected'
        assert_paired(store.messages(agent.session.id))
    finally:
        await agent.llm.http.aclose(); store.close()


async def test_child_extends_its_own_blocks_and_reports_loop_to_parent(tmp_path):
    script = [{'tool_calls': [tool_call('child', 'subagent', task='Проверить')]}]
    script += [response(i) for i in range(6)] + [{'content': 'Учту ошибку подзадачи'}]
    agent, store, _ = make_agent(tmp_path, script, max_steps=2, subagents={'max_steps': 1}, permissions={'mode': 'allow_all'})
    agent.registry.add(Subagent())
    try:
        events = await run(agent)
        assert events[-1]['reason'] == 'stop'
        assert next(e for e in events if e['type'] == 'subagent_end')['reason'] == 'loop_detected'
        assert not next(e for e in events if e['type'] == 'tool_result' and e['id'] == 'child')['ok']
        child_steps = [e['event'] for e in events if e['type'] == 'subagent_event' and e['event']['type'] == 'step']
        assert [e['total'] for e in child_steps] == list(range(1, 7))
        assert all(e['current'] == e['limit'] == 1 for e in child_steps)
        for sid in {agent.session.id, next(e['session_id'] for e in events if e['type'] == 'subagent_start')}:
            assert_paired(store.messages(sid))
    finally:
        await agent.llm.http.aclose(); store.close()


@pytest.mark.parametrize('tool', ['fail', 'danger'])
async def test_repeated_errors_and_denials_are_detected(tmp_path, tool):
    agent, store, _ = make_agent(tmp_path, [{'tool_calls': [tool_call(f'e{i}', tool)]} for i in range(6)])
    try:
        events = await run(agent)
        assert events[-1]['reason'] == 'loop_detected'
        assert all(not e['ok'] for e in events if e['type'] == 'tool_result')
        assert_paired(store.messages(agent.session.id))
    finally:
        await agent.llm.http.aclose(); store.close()


async def test_effective_hook_arguments_are_used(tmp_path):
    command = hook_cmd("import json; print(json.dumps({'updated_input': {'text': 'одинаково'}}))")
    agent, store, _ = make_agent(tmp_path, [response(i, str(i)) for i in range(6)],
                                  hooks={'PreToolUse': [{'command': command}]})
    try:
        events = await run(agent)
        assert events[-1]['reason'] == 'loop_detected'
        assert all(t['args'] == {'text': 'одинаково'} for t in store.tool_runs(agent.session.id))
    finally:
        await agent.llm.http.aclose(); store.close()


async def test_child_timeout_survives_block_extension(tmp_path):
    script = [{'tool_calls': [tool_call('child', 'subagent', task='Проверить')]}]
    script += [response(0), {'tool_calls': [tool_call('slow-child', 'slow')]}, {'content': 'Подзадача не завершена'}]
    agent, store, _ = make_agent(tmp_path, script, subagents={'max_steps': 1, 'timeout': .5}, permissions={'mode': 'allow_all'})
    agent.registry.add(Subagent())
    try:
        events = await run(agent)
        child = next(e for e in events if e['type'] == 'subagent_end')
        assert child['reason'] == 'timeout' and events[-1]['reason'] == 'stop'
        assert any(e['type'] == 'subagent_event' and e['event']['type'] == 'step' and e['event']['extended'] for e in events)
        assert_paired(store.messages(child['session_id']))
    finally:
        await agent.llm.http.aclose(); store.close()
