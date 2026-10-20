"""Проверки доступности без исполнения произвольных команд."""
import json
import shutil

from .base import Tool, ToolError
from .files import resolve
from .web import request


class Inspect(Tool):
    name = 'inspect'
    plan_safe = True
    default_permission = 'allow'
    description = ('Проверить факт доступности: program — наличие программы в PATH без запуска; path — наличие и тип пути внутри проекта; '
                   'http — HTTP HEAD с кодом ответа и конечным URL. HEAD 405/501 означает отсутствие поддержки проверки, а не недоступность сервиса; для чтения используй web_fetch. Не исполняет shell.')
    parameters = {'type': 'object', 'properties': {
        'kind': {'type': 'string', 'enum': ['program', 'path', 'http']},
        'target': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
    }, 'required': ['kind', 'target'], 'additionalProperties': False}

    def check_args(self, args):
        super().check_args(args)
        if not args['target'].strip() or len(args['target']) > 4000:
            raise ToolError('цель проверки должна содержать от 1 до 4000 символов')

    def subject(self, args):
        return args.get('target')

    async def run(self, args, ctx):
        kind, target = args['kind'], args['target']
        result = {'kind': kind, 'target': target}
        if kind == 'program':
            # Только имя в PATH; исполняемый файл не запускается.
            if '/' in target or '\\' in target or target in ('.', '..'):
                raise ToolError('program принимает имя программы в PATH без пути')
            found = shutil.which(target)
            result.update(available=bool(found), path=found)
        elif kind == 'path':
            path = resolve(ctx, target)
            result.update(available=path.exists(), type='directory' if path.is_dir() else 'file' if path.is_file() else 'other' if path.exists() else 'missing')
        else:
            response, _, _ = await request(ctx, 'HEAD', target)
            result.update(status=response.status_code, url=str(response.url),
                          reachable=True, available=None if response.status_code in (401, 403, 405, 501) else 200 <= response.status_code < 400,
                          requires_auth=response.status_code in (401, 403),
                          head_supported=response.status_code not in (405, 501))
        return json.dumps(result, ensure_ascii=False)


TOOLS = [Inspect]
