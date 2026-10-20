"""Агенты build/plan и уточнения пользователя без повторного исполнения действий."""
import asyncio
import json

from agent.runtime import make_registry
from agent.settings import DEFAULTS, merge
from agent.store import SessionStore
from agent.tools.files import ReadFile, WriteFile
from agent.tools.question import Question
from tests.fakes import tool_call
from tests.test_loop import make_agent, run, hook_cmd


def test_plan_registry_contains_only_safe_tools():
    plan = make_registry(DEFAULTS, agent_mode='plan')
    assert {'read_file', 'grep', 'goal', 'todo', 'question', 'subagent', 'inspect'} <= set(plan.names())
    assert not {'shell', 'write_file', 'edit_file', 'http_request'} & set(plan.names())
    build = make_registry(DEFAULTS)
    assert {'shell', 'write_file', 'question'} <= set(build.names())


async def test_plan_cannot_write_even_with_allow_all_and_allowing_hook(tmp_path):
    seen = []
    agent, store, _ = make_agent(tmp_path, [{'tool_calls': [tool_call('write', 'write_file', path='forbidden', content='x')]},
                                           {'content': 'Составлен план'}], seen,
                                permissions={'mode': 'allow_all'},
                                hooks={'PreToolUse': [{'command': hook_cmd("print('{\"decision\":\"allow\"}')")}]})
    agent.registry.add(WriteFile())
    store.update(agent.session.id, agent_mode='plan'); agent.session = store.get(agent.session.id)
    events = await run(agent, 'Исследуй и составь план')
    assert events[-1]['reason'] == 'stop'
    assert not (tmp_path / 'forbidden').exists()
    result = next(event for event in events if event['type'] == 'tool_result')
    assert not result['ok'] and 'plan' in result['content']
    assert 'write_file' not in {t['function']['name'] for t in seen[0].get('tools', [])}
    assert 'Активный агент: plan' in seen[0]['messages'][0]['content']
    await agent.llm.http.aclose(); store.close()


async def test_question_waits_for_answer_then_continues_same_request(tmp_path):
    seen, requests = [], []
    agent, store, _ = make_agent(tmp_path, [{'tool_calls': [tool_call('q', 'question', question='Какой формат?', options=['JSON', 'CSV'])]},
                                           {'content': 'Использую CSV'}], seen)
    agent.registry.add(Question())
    gate = asyncio.Event()
    async def questioner(request):
        requests.append(request)
        await gate.wait()
        return 'CSV'
    agent.questioner = questioner
    task = asyncio.create_task(run(agent, 'Экспортируй'))
    for _ in range(100):
        if requests:
            break
        await asyncio.sleep(.01)
    assert requests and not task.done() and len(seen) == 1
    gate.set(); events = await task
    assert events[-1]['reason'] == 'stop' and len(seen) == 2
    assert requests[0]['request_id'] == events[-1]['request_id']
    assert json.loads(next(m['content'] for m in seen[-1]['messages'] if m['role'] == 'tool')) == {'answer': 'CSV'}
    await agent.llm.http.aclose(); store.close()


async def test_cancelled_or_unavailable_question_does_not_hang(tmp_path):
    for name, callback in [('cancel', True), ('unavailable', False)]:
        folder = tmp_path / name; folder.mkdir()
        agent, store, _ = make_agent(folder, [{'tool_calls': [tool_call('q', 'question', question='Уточнить?')]}, {'content': 'Продолжаю'}])
        agent.registry.add(Question())
        if callback:
            async def questioner(request):
                return None
            agent.questioner = questioner
        events = await run(agent)
        assert events[-1]['reason'] == 'stop'
        result = next(e for e in events if e['type'] == 'tool_result')
        assert ('cancelled' in result['content']) if callback else not result['ok']
        await agent.llm.http.aclose(); store.close()


def test_agent_mode_persists_and_shared_history_is_preserved(tmp_path):
    path = tmp_path / 's.db'
    store = SessionStore(path); session = store.create('local', agent_mode='plan')
    store.add_message(session.id, {'role': 'user', 'content': 'Исходная задача'})
    store.update(session.id, agent_mode='build'); store.close()
    store = SessionStore(path)
    assert store.get(session.id).agent_mode == 'build'
    assert store.messages(session.id)[0]['content'] == 'Исходная задача'
    assert store.get(session.id).to_dict()['agent_mode'] == 'build'
    store.close()
