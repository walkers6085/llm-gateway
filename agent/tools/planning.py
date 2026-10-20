"""Цель и план сессии; состояние фиксируется метаданными результата инструмента."""
import json

from .base import Tool, ToolError


class Goal(Tool):
    plan_safe = True
    name = 'goal'
    description = 'Прочитать текущую цель или задать уточнённую цель сессии. Без text — чтение. Сохраняй намерение пользователя.'
    default_permission = 'allow'
    parameters = {'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 4000}}, 'additionalProperties': False}

    def check_args(self, args):
        super().check_args(args)
        if len(args.get('text', '')) > 4000:
            raise ToolError('цель длиннее 4000 символов')

    async def run(self, args, ctx):
        state = ctx.extra['planning_state']()
        return json.dumps({'goal': args.get('text', state['goal'])}, ensure_ascii=False)


class Todo(Tool):
    plan_safe = True
    name = 'todo'
    description = 'Прочитать список задач или заменить его целиком через items. Обновляй статусы по мере работы, сохраняй завершённые пункты.'
    default_permission = 'allow'
    parameters = {'type': 'object', 'properties': {'items': {'type': 'array', 'maxItems': 20, 'items': {
        'type': 'object', 'properties': {
            'id': {'type': 'string', 'minLength': 1, 'maxLength': 40},
            'text': {'type': 'string', 'minLength': 1, 'maxLength': 500},
            'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed', 'cancelled']},
        }, 'required': ['id', 'text', 'status'], 'additionalProperties': False}}}, 'additionalProperties': False}

    def check_args(self, args):
        super().check_args(args)
        items = args.get('items', [])
        if len(items) > 20:
            raise ToolError('допустимо не больше 20 задач')
        if any(len(item['id']) > 40 or len(item['text']) > 500 or not item['text'].strip() for item in items):
            raise ToolError('задача должна иметь непустой текст до 500 символов и идентификатор до 40 символов')
        ids = [item['id'] for item in items]
        if len(ids) != len(set(ids)):
            raise ToolError('идентификаторы задач должны быть уникальными')

    async def run(self, args, ctx):
        state = ctx.extra['planning_state']()
        return json.dumps({'todos': args.get('items', state['todos'])}, ensure_ascii=False)


class Statistics(Tool):
    plan_safe = True
    name = 'statistics'
    description = 'Прочитать статистику текущей сессии: токены, вызовы моделей, время, инструменты, ошибки и повторы. Только агрегаты без текста запросов.'
    default_permission = 'allow'
    parameters = {'type': 'object', 'properties': {}, 'additionalProperties': False}

    async def run(self, args, ctx):
        return json.dumps(ctx.extra['statistics'](), ensure_ascii=False)


TOOLS = [Goal, Todo, Statistics]
