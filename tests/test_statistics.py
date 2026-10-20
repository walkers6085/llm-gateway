"""Названия сессий и статистика владельца без вызовов внешних моделей."""
import json

import pytest

from agent.store import SessionStore, task_title
from agent.runtime import make_registry, make_agent
from agent.settings import DEFAULTS, merge


def test_titles_use_task_after_greeting_and_keep_manual_name(tmp_path):
    store = SessionStore(tmp_path / 'sessions.db')
    try:
        session = store.create('owner')
        store.add_message(session.id, {'role': 'user', 'content': 'Привет'})
        assert store.ensure_title(session.id) == 'Привет'
        store.add_message(session.id, {'role': 'user', 'content': 'Переделать меню провайдеров и выбор моделей'})
        original_updated = store.get(session.id).updated
        title = store.list('owner')[0].title
        assert title == 'Переделать меню провайдеров и выбор моделей'
        assert store.get(session.id).updated == original_updated
        store.update(session.id, title='Мой проект', title_auto=0)
        assert store.ensure_title(session.id, 'Другой запрос') == 'Мой проект'
        assert store.ensure_title(session.id, force=True) == title
        assert task_title('# Задача\n```sh\nrm ignored\n```') == 'Задача'
        assert task_title('128 тысяч токенов') == '128 тысяч токенов'
    finally:
        store.close()


def test_statistics_counts_attempts_once_and_isolates_owner(tmp_path):
    store = SessionStore(tmp_path / 'sessions.db')
    try:
        own = store.create('owner')
        other = store.create('other')
        for sid in (own.id, other.id):
            for event, data in [
                ('request.start', {}), ('model.start', {'model': 'alpha'}),
                ('model.end', {'model': 'alpha', 'status': 200, 'prompt_tokens': 100, 'completion_tokens': 20, 'ms': 1000}),
                ('model.end', {'model': 'alpha', 'status': 429, 'prompt_tokens': 0, 'completion_tokens': 0, 'ms': 500}),
                ('model.retry', {}), ('context.compacted', {'method': 'model'}),
                ('context.compacted', {'estimated': 50}),
            ]:
                store.trace('request-' + sid, 'span', '', sid, event, data)
            store.add_message(sid, {'role': 'user', 'content': 'Запрос'})
            store.record_tool(sid, 'call', 'read_file', {}, 'allow', True, 25)
        stats = store.statistics('owner')
        assert stats['sessions'] == stats['requests'] == stats['messages'] == 1
        assert stats['total_tokens'] == 120 and stats['model_calls'] == 2
        assert stats['errors'] == stats['retries'] == stats['compactions'] == stats['unknown_usage'] == 1
        assert stats['tool_calls'] == 1 and stats['tools'][0]['tool'] == 'read_file'
        assert stats['models'][0]['tokens_per_second'] == 13.3
        assert store.session_summaries('owner')[own.id]['tokens'] == 120
        assert other.id not in store.session_summaries('owner')
        with pytest.raises(ValueError, match='не найдена'):
            store.statistics('owner', other.id)
        assert store.statistics('nobody')['model_calls'] == 0
    finally:
        store.close()


async def test_statistics_tool_is_read_only_and_scoped(tmp_path):
    store = SessionStore(tmp_path / 'sessions.db')
    try:
        session = store.create('owner', workspace=str(tmp_path))
        settings = merge(DEFAULTS, {'permissions': {'mode': 'read_only'}})
        agent = make_agent(llm=None, settings=settings, store=store, session=session)
        tool = make_registry(settings, agent_mode='plan').get('statistics')
        assert tool is not None and tool.plan_safe and tool.default_permission == 'allow'
        stats = json.loads(await tool.run({}, agent.ctx))
        assert stats['sessions'] == 1 and stats['total_tokens'] == 0
        with pytest.raises(Exception):
            tool.check_args({'session_id': 'another'})
    finally:
        store.close()


def test_statistics_includes_children_without_double_count_or_foreign_leak(tmp_path):
    store = SessionStore(tmp_path / 'sessions.db')
    try:
        parent, child, foreign = store.create('owner'), store.create('owner'), store.create('other')
        store.trace('r', 'root', '', parent.id, 'request.start', {})
        store.trace('r', 'child', 'root', child.id, 'request.start', {'delegated': True})
        for session in (parent, child, foreign):
            store.trace('r', 'model', 'child', session.id, 'model.end',
                        {'model': 'alpha', 'status': 200, 'prompt_tokens': 10, 'completion_tokens': 2})
        assert store.statistics('owner', parent.id)['total_tokens'] == 24
        assert store.statistics('owner', child.id)['total_tokens'] == 12
        assert store.statistics('owner')['total_tokens'] == 24
    finally:
        store.close()
