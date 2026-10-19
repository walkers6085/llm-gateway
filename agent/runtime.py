"""Сборка агента по настройкам: встроенные инструменты + MCP + хуки. Общая для CLI и сервера."""

import os

from .hooks import Hooks
from .llm import LLMClient
from .loop import Agent, Approver
from .mcp import MCPManager
from .settings import SettingsError
from .store import Session, SessionStore
from .tools import files, shell, web
from .tools.base import Registry, Tool

BUILTIN: dict[str, type[Tool]] = {cls.name: cls for cls in files.TOOLS + shell.TOOLS + web.TOOLS}


def builtin_tools(enabled: list[str]) -> list[Tool]:
    unknown = [n for n in enabled if n not in BUILTIN]
    if unknown:
        raise SettingsError(f"tools.enabled: неизвестные инструменты: {', '.join(unknown)}; "
                            f"есть: {', '.join(BUILTIN)}")
    return [BUILTIN[n]() for n in enabled]


def make_registry(settings: dict, mcp: MCPManager | None = None) -> Registry:
    registry = Registry(builtin_tools(settings["tools"]["enabled"]))
    for tool in (mcp.tools if mcp else []):
        if registry.get(tool.name) is None:
            registry.add(tool)
    return registry


async def start_mcp(settings: dict) -> MCPManager:
    return await MCPManager(settings["mcp"], proxy=settings["network"]["proxy"]).start()


def make_agent(*, llm: LLMClient, settings: dict, store: SessionStore, session: Session,
               mcp: MCPManager | None = None, approver: Approver | None = None, on_usage=None,
               hook_env: dict | None = None) -> Agent:
    """hook_env — дополнительные переменные окружения для хуков (сервер передаёт GATEWAY_ROOT)."""
    hooks = Hooks(settings["hooks"], cwd=session.workspace, session_id=session.id,
                  env={**os.environ, **(hook_env or {})})
    return Agent(llm=llm, registry=make_registry(settings, mcp), settings=settings, store=store,
                 session=session, hooks=hooks, approver=approver, on_usage=on_usage)
