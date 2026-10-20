"""Статистика по журналу: без содержимого запросов, ответов и аргументов инструментов."""
import json


def summarize(events, tool_runs, *, sessions, messages):
    result = dict(sessions=sessions, messages=messages, requests=0, model_calls=0, errors=0,
                  retries=0, compactions=0, prompt_tokens=0, completion_tokens=0,
                  model_ms=0, unknown_usage=0, tools=[], models=[])
    models, tools = {}, {}
    for row in events:
        event, data = row['event'], json.loads(row['data'])
        if event == 'request.start' and not data.get('delegated'):
            result['requests'] += 1
        if event == 'model.retry':
            result['retries'] += 1
        if event == 'context.compacted' and data.get('method') == 'model':
            result['compactions'] += 1
        if event != 'model.end':
            continue
        model = str(data.get('model') or 'неизвестна')
        item = models.setdefault(model, dict(model=model, calls=0, errors=0, prompt_tokens=0, completion_tokens=0, ms=0))
        item['calls'] += 1
        item['errors'] += data.get('status') != 200
        item['prompt_tokens'] += data.get('prompt_tokens', 0)
        item['completion_tokens'] += data.get('completion_tokens', 0)
        item['ms'] += data.get('ms', 0)
        result['unknown_usage'] += not (data.get('prompt_tokens') or data.get('completion_tokens'))
    for row in tool_runs:
        item = tools.setdefault(row['tool'], dict(tool=row['tool'], calls=0, errors=0, ms=0))
        item['calls'] += 1
        item['errors'] += not row['ok']
        item['ms'] += row['ms']
    for item in models.values():
        result['model_calls'] += item['calls']
        result['errors'] += item['errors']
        result['prompt_tokens'] += item['prompt_tokens']
        result['completion_tokens'] += item['completion_tokens']
        result['model_ms'] += item['ms']
        item['tokens_per_second'] = round(item['completion_tokens'] * 1000 / item['ms'], 1) if item['ms'] and item['completion_tokens'] else None
    result['total_tokens'] = result['prompt_tokens'] + result['completion_tokens']
    result['tool_calls'] = sum(item['calls'] for item in tools.values())
    result['tool_errors'] = sum(item['errors'] for item in tools.values())
    result['models'] = sorted(models.values(), key=lambda item: -item['calls'])
    result['tools'] = sorted(tools.values(), key=lambda item: -item['calls'])
    return result


def tables(stats):
    """Одинаковые таблицы для CLI и OpenTUI, в том числе пустого журнала."""
    yield 'Статистика', ['Показатель', 'Значение'], [
        ['Сессии', stats['sessions']], ['Запросы пользователя (по журналу)', stats['requests']],
        ['Сообщения пользователя', stats['messages']], ['Вызовы модели', stats['model_calls']],
        ['Токены вход / выход', f"{stats['prompt_tokens']:,} / {stats['completion_tokens']:,}"],
        ['Всего токенов', f"{stats['total_tokens']:,}"], ['Время вызовов модели, с', round(stats['model_ms'] / 1000, 1)],
        ['Ошибки модели / повторы', f"{stats['errors']} / {stats['retries']}"],
        ['Суммаризации', stats['compactions']], ['Инструменты / ошибки', f"{stats['tool_calls']} / {stats['tool_errors']}"],
        ['Вызовы без данных о токенах', stats['unknown_usage']],
    ]
    if stats['models']:
        yield 'По моделям', ['Модель', 'Вызовы', 'Вход', 'Выход', 'Ошибки', 'Секунды', 'Ток/с'], [
            [m['model'], m['calls'], m['prompt_tokens'], m['completion_tokens'], m['errors'], round(m['ms']/1000, 1), m['tokens_per_second'] if m['tokens_per_second'] is not None else 'н/д'] for m in stats['models']]
    if stats['tools']:
        yield 'По инструментам', ['Инструмент', 'Вызовы', 'Ошибки', 'Секунды'], [
            [t['tool'], t['calls'], t['errors'], round(t['ms']/1000, 1)] for t in stats['tools']]
