"""Базовые типы инструментов: Tool, ToolContext, реестр."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..schema import SchemaError, validate


class ToolError(Exception):
    """Ожидаемая ошибка инструмента: текст уходит модели как результат с is_error."""


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


@dataclass
class ToolContext:
    workspace: Path
    settings: dict
    session_id: str = ""
    extra: dict = field(default_factory=dict)


class Tool:
    """Инструмент агента.

    name/description/parameters уходят модели как OpenAI function tool.
    default_permission — что делать без явных правил (`allow` или `ask`), см. permissions.py.
    """

    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}
    default_permission: str = "ask"
    plan_safe: bool = False

    def subject(self, args: dict) -> str | None:
        """Строка, с которой сравнивается шаблон правила `tool(шаблон)`; None — шаблоны не применимы."""
        return None

    def always_rule(self, args: dict) -> str:
        """Правило, которое добавляется при ответе «всегда» на запрос подтверждения."""
        subject = self.subject(args)
        return f"{self.name}({subject})" if subject else self.name

    def spec(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}

    def check_args(self, args: dict) -> None:
        try:
            validate(args, self.parameters)
        except SchemaError as exc:
            raise ToolError(f"неверные аргументы: {exc}") from None

    async def run(self, args: dict, ctx: ToolContext) -> str | ToolResult:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class Registry:
    def __init__(self, tools: list[Tool] | None = None):
        self.tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.add(tool)

    def add(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"инструмент {tool.name!r} уже зарегистрирован")
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return list(self.tools)

    def specs(self) -> list[dict]:
        return [t.spec() for t in self.tools.values()]


def parse_arguments(raw: str | dict | None) -> dict:
    """Аргументы от модели: JSON-строка (OpenAI) или уже словарь; пусто — {}."""
    if isinstance(raw, dict):
        return raw
    if not raw or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ToolError(f"аргументы — не JSON: {exc}") from None
    if not isinstance(value, dict):
        raise ToolError("аргументы должны быть JSON-объектом")
    return value


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n…[обрезано {len(text) - limit} символов]…\n{text[-tail:]}"


def normalize_rel(path: str) -> str:
    return os.path.normpath(path).lstrip("/") if path else "."
