"""Планирование сессии, контекст и восстановление после отката без новой схемы БД."""
import json

import pytest

from agent.runtime import make_registry
from agent.settings import DEFAULTS
from agent.store import SessionStore
from agent.tools.base import ToolError
from agent.tools.planning import Goal, Todo
from tests.fakes import tool_call
from tests.test_loop import make_agent, run


def test_planning_tools_registered_and_bounded():
    registry = make_registry(DEFAULTS)
    assert isinstance(registry.get('goal'), Goal)
    assert isinstance(registry.get('todo'), Todo)
    with pytest.raises(ToolError):
        Goal().check_args({'text': 'a' * 4001})
    for items in ([{'id': 'a', 'text': 'x', 'status': 'bad'}],
                  [{'id': 'a', 'text': 'x', 'status': 'pending'}] * 21,
                  [{'id': 'a', 'text': 'x' * 501, 'status': 'pending'}],
                  [{'id': 'a', 'text': 'x', 'status': 'pending'}] * 2):
        with pytest.raises(ToolError):
            Todo().check_args({'items': items})


async def test_planning_persist_compact_read_and_revert(tmp_path):
    todo = [{'id': 'test', 'text': 'Проверить Unicode', 'status': 'in_progress'}]
    script = [{'tool_calls': [tool_call('g1', 'goal', text='Починить CLI'), tool_call('t1', 'todo', items=todo)]},
              {'content': 'Работаю'},
              {'tool_calls': [tool_call('g2', 'goal', text='Другая цель'), tool_call('t2', 'todo', items=[])]},
              {'content': 'Готово'}]
    seen = []
    agent, store, _ = make_agent(tmp_path, script, seen)
    agent.registry.add(Goal()); agent.registry.add(Todo())
    events = await run(agent, 'Исходный запрос')
    assert events[-1]['reason'] == 'stop'
    expected = {'goal': 'Починить CLI', 'todos': todo}
    assert store.planning_state(agent.session.id) == expected
    assert Goal().default_permission == 'allow'
    assert json.loads(await Todo().run({}, agent.ctx)) == {'todos': todo}
    assert store.compact(agent.session.id)
    messages, _, _ = agent._context(agent.registry.specs(), 32768)
    assert 'Починить CLI' in json.dumps(messages, ensure_ascii=False)
    assert 'in_progress' in json.dumps(messages)
    events = await run(agent, 'Новый запрос')
    assert events[-1]['reason'] == 'stop'
    row = next(row for row in store.timeline(agent.session.id) if row['metadata'].get('user_input') == 'Новый запрос')
    store.rewind(agent.session.id, row['seq'], revision=store.revision(agent.session.id))
    assert store.planning_state(agent.session.id) == expected
    await agent.llm.http.aclose()
    store.close()
    reopened = SessionStore(tmp_path / 's.db')
    assert reopened.planning_state(agent.session.id) == expected
    reopened.close()


async def test_denied_planning_tool_does_not_change_state(tmp_path):
    agent, store, _ = make_agent(tmp_path, [{'tool_calls': [tool_call('g', 'goal', text='Подмена')]},
                                           {'content': 'Готово'}], permissions={'deny': ['goal']})
    agent.registry.add(Goal())
    await run(agent, 'Настоящая задача')
    assert store.planning_state(agent.session.id)['goal'] == 'Настоящая задача'
    assert not any('planning' in row['metadata'] for row in store.timeline(agent.session.id))
    await agent.llm.http.aclose(); store.close()


async def test_context_event_matches_request_and_preview_does_not_compact(tmp_path):
    seen = []
    agent, store, _ = make_agent(tmp_path, [{'content': 'Готово'}], seen,
                                model_profiles={'alpha': {'max_input_tokens': 10000, 'max_output_tokens': 500}})
    store.add_message(agent.session.id, {'role': 'user', 'content': 'старая задача'})
    store.add_message(agent.session.id, {'role': 'assistant', 'content': 'большой ответ ' * 1000})
    before = store.get(agent.session.id).context_seq
    status = agent.context_status()
    assert status['estimated_tokens'] > 10000
    assert store.get(agent.session.id).context_seq == before
    events = await run(agent, 'Последний запрос')
    context = next(event for event in events if event['type'] == 'context')
    assert context['max_input_tokens'] == 10000 and context['max_output_tokens'] == 500
    size = len(json.dumps([seen[0]['messages'], seen[0]['tools']], ensure_ascii=False).encode()) + 64
    assert context['estimated_tokens'] == size
    await agent.llm.http.aclose(); store.close()


@pytest.mark.parametrize('blocked', [False, True])
async def test_post_hook_context_or_block_keeps_planning_consistent(tmp_path, blocked):
    from tests.test_loop import hook_cmd
    output = {'decision': 'deny', 'reason': 'запрещено'} if blocked else {'additional_context': 'Проверено хуком'}
    command = hook_cmd('print(' + repr(json.dumps(output)) + ')')
    agent, store, _ = make_agent(tmp_path, [{'tool_calls': [tool_call('g', 'goal', text='Новая цель')]},
                                           {'content': 'Готово'}],
                                hooks={'PostToolUse': [{'matcher': 'goal', 'command': command}]})
    agent.registry.add(Goal())
    events = await run(agent, 'Исходная цель')
    assert events[-1]['reason'] == 'stop'
    assert store.planning_state(agent.session.id)['goal'] == ('Исходная цель' if blocked else 'Новая цель')
    await agent.llm.http.aclose(); store.close()
