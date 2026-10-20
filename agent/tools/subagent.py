"""Описание ограниченной дочерней задачи; выполнение обеспечивает цикл агента."""

from .base import Tool, ToolError

class Subagent(Tool):
    name = "subagent"
    description = "Поручить отдельному агенту самостоятельную подзадачу с отдельной историей. Вложенные агенты запрещены."
    default_permission = "ask"
    parameters = {"type": "object", "properties": {"task": {"type": "string", "minLength": 1, "maxLength": 16000}},
                  "required": ["task"], "additionalProperties": False}

    def check_args(self, args):
        super().check_args(args)
        if len(args["task"]) > 16000:
            raise ToolError("задача сабагента длиннее 16000 символов")

    async def run(self, args, ctx):
        raise ToolError("сабагент доступен только внутри цикла агента")
