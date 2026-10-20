"""Уточняющий вопрос модели пользователю через интерактивный интерфейс."""
import json

from .base import Tool, ToolError
from ..tracing import identifier


class Question(Tool):
    name = 'question'
    description = 'Запросить уточнение пользователя и дождаться ответа. Предложи варианты options или задай открытый вопрос. Свободный ответ доступен всегда.'
    default_permission = 'allow'
    plan_safe = True
    parameters = {'type': 'object', 'properties': {
        'question': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
        'options': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
    }, 'required': ['question'], 'additionalProperties': False}

    def check_args(self, args):
        super().check_args(args)
        options = args.get('options', [])
        if not args['question'].strip() or len(args['question']) > 4000:
            raise ToolError('вопрос должен содержать от 1 до 4000 символов')
        if len(options) > 8 or any(not item.strip() or len(item) > 200 for item in options):
            raise ToolError('допустимо до 8 непустых вариантов по 200 символов')
        if len(set(options)) != len(options):
            raise ToolError('варианты ответа должны различаться')

    async def run(self, args, ctx):
        answer = await ctx.extra['questioner']({'id': identifier(), **args})
        return json.dumps({'cancelled': True} if answer is None else {'answer': answer}, ensure_ascii=False)


TOOLS = [Question]
