"""Агент на сервере: /agent/* поверх ядра agent/ (сессии, SSE событий, подтверждения).

Модель вызывается напрямую через mTLS-клиент шлюза (без круга через /v1), расход пишется
в ту же таблицу usage с путём `agent`. Доступ — только токенам с agent_profile.
Справка по API — docs/wiki/server-api.md.
"""

import asyncio
import json
import logging
import shutil
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent.llm import LLMClient
from agent.loop import Agent
from agent.mcp import MCPManager
from agent.runtime import make_agent, make_registry, start_mcp
from agent.settings import SettingsError, expand, load_profiles
from agent.store import Session, SessionStore

from .config import ROOT, Settings
from .tokens import Token, TokenStore

log = logging.getLogger("gateway.agent")

HEARTBEAT = 15  # с: комментарий в SSE, пока агент ждёт подтверждения, — чтобы прокси не рвали поток
MAX_CACHED_AGENTS = 200


class NewSession(BaseModel):
    model: str = ""
    title: str = ""


class NewMessage(BaseModel):
    content: str


class Approval(BaseModel):
    id: str
    decision: str  # allow | deny | always


class Run:
    """Идущий запрос к агенту: задача, очередь событий для SSE, ожидающие подтверждения."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.pending: dict[str, asyncio.Future] = {}
        self.task: asyncio.Task | None = None

    async def approver(self, request: dict) -> str:
        fut = asyncio.get_running_loop().create_future()
        self.pending[request["id"]] = fut
        try:
            return await fut
        finally:
            self.pending.pop(request["id"], None)


def sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


class AgentService:
    def __init__(self, settings: Settings, tokens: TokenStore,
                 client: Callable[[], httpx.AsyncClient], upstream_headers: Callable[[], dict]):
        self.settings, self.tokens = settings, tokens
        self.client, self.upstream_headers = client, upstream_headers
        self.sessions = SessionStore(settings.db_path)
        self.profiles: dict[str, dict] | None = None
        self.config_error = ""
        self.mcp: dict[str, MCPManager] = {}
        self.mcp_lock = asyncio.Lock()
        self.runs: dict[str, Run] = {}
        self.agents: OrderedDict[str, Agent] = OrderedDict()
        self.reload()

    def reload(self) -> None:
        try:
            self.profiles, self.config_error = load_profiles(self.settings.agent_config), ""
        except SettingsError as exc:
            self.profiles, self.config_error = None, str(exc)
            log.error("Агент не настроен: %s", exc)

    def profile(self, token: Token, error_cls) -> dict:
        if self.profiles is None:
            raise error_cls(503, "Агент не настроен: " + self.config_error, "server_error", "agent_not_configured")
        if not token.agent_profile:
            raise error_cls(403, "Агент недоступен для этого токена", "permission_error", "agent_not_allowed")
        if token.agent_profile not in self.profiles:
            raise error_cls(403, f"Профиль агента {token.agent_profile!r} не найден в config/agent.json",
                            "permission_error", "agent_profile_missing")
        return self.profiles[token.agent_profile]

    async def mcp_for(self, name: str, profile: dict) -> MCPManager | None:
        if not profile["mcp"]["servers"]:
            return None
        async with self.mcp_lock:
            if name not in self.mcp:
                self.mcp[name] = await start_mcp(profile)
        return self.mcp[name]

    def workspace(self, profile: dict, token: Token, session_id: str) -> Path:
        path = expand(profile["workspace_root"], ROOT) / str(token.id) / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def agent_for(self, session: Session, token: Token, profile: dict, run: Run) -> Agent:
        agent = self.agents.get(session.id)
        if agent is None:
            llm = LLMClient(self.client(), headers=self.upstream_headers())
            agent = make_agent(
                llm=llm, settings=profile, store=self.sessions, session=session,
                mcp=await self.mcp_for(token.agent_profile, profile),
                hook_env={"GATEWAY_ROOT": str(ROOT)},
                on_usage=lambda model, status, usage: self.tokens.record(
                    token.id, "agent", model, status, usage[0], usage[1]))
            self.agents[session.id] = agent
            while len(self.agents) > MAX_CACHED_AGENTS:
                self.agents.popitem(last=False)
        self.agents.move_to_end(session.id)
        agent.session = session            # свежие allow_rules/модель из базы
        agent.approver = run.approver
        return agent

    async def close(self) -> None:
        for run in list(self.runs.values()):
            if run.task:
                run.task.cancel()
        await asyncio.gather(*(r.task for r in self.runs.values() if r.task), return_exceptions=True)
        await asyncio.gather(*(m.close() for m in self.mcp.values()), return_exceptions=True)
        self.mcp.clear()


def mount(app: FastAPI, service: AgentService, authenticate, upstream, error_cls) -> None:
    def session_or_404(session_id: str, token: Token) -> Session:
        session = service.sessions.get(session_id, owner=str(token.id))
        if session is None:
            raise error_cls(404, "Сессия не найдена", code="session_not_found")
        return session

    @app.post("/agent/sessions")
    async def create_session(req: NewSession, token: Token = Depends(authenticate)):
        profile = service.profile(token, error_cls)
        model = req.model or profile["model"]
        if not model:
            raise error_cls(400, "Нужна модель: поле model", code="model_required")
        if not token.allows(model):
            raise error_cls(403, f"Модель {model!r} недоступна для этого токена", "permission_error", "model_not_allowed")
        session = service.sessions.create(str(token.id), title=req.title, profile=token.agent_profile, model=model)
        ws = service.workspace(profile, token, session.id)
        service.sessions.update(session.id, workspace=str(ws))
        return service.sessions.get(session.id).to_dict()

    @app.get("/agent/sessions")
    async def list_sessions(token: Token = Depends(authenticate)):
        service.profile(token, error_cls)
        return {"data": [{**s.to_dict(), "running": s.id in service.runs}
                         for s in service.sessions.list(str(token.id))]}

    @app.get("/agent/sessions/{session_id}")
    async def get_session(session_id: str, token: Token = Depends(authenticate)):
        service.profile(token, error_cls)
        session = session_or_404(session_id, token)
        run = service.runs.get(session_id)
        return {**session.to_dict(), "running": run is not None,
                "pending": list(run.pending) if run else [],
                "messages": service.sessions.messages(session_id),
                "tool_runs": service.sessions.tool_runs(session_id)}

    @app.delete("/agent/sessions/{session_id}")
    async def delete_session(session_id: str, token: Token = Depends(authenticate)):
        profile = service.profile(token, error_cls)
        session = session_or_404(session_id, token)
        run = service.runs.get(session_id)
        if run and run.task:
            run.task.cancel()
        service.sessions.delete(session_id)
        service.agents.pop(session_id, None)
        root = expand(profile["workspace_root"], ROOT).resolve()
        ws = Path(session.workspace).resolve() if session.workspace else None
        if ws and root in ws.parents:  # удаляем только папки внутри workspace_root
            shutil.rmtree(ws, ignore_errors=True)
        return {"deleted": session_id}

    @app.post("/agent/sessions/{session_id}/messages")
    async def send_message(session_id: str, req: NewMessage, token: Token = Depends(authenticate)):
        profile = service.profile(token, error_cls)
        session = session_or_404(session_id, token)
        if not token.allows(session.model):
            raise error_cls(403, f"Модель {session.model!r} недоступна для этого токена",
                            "permission_error", "model_not_allowed")
        if not req.content.strip():
            raise error_cls(400, "Пустое сообщение", code="empty_message")
        if session_id in service.runs:
            raise error_cls(409, "Сессия уже выполняет запрос", code="session_busy")
        upstream()  # 503, если шлюз не настроен
        Path(session.workspace).mkdir(parents=True, exist_ok=True)
        run = Run()
        service.runs[session_id] = run
        try:
            agent = await service.agent_for(session, token, profile, run)
        except Exception:
            service.runs.pop(session_id, None)
            raise

        async def produce():
            try:
                async for event in agent.run(req.content):
                    await run.queue.put(event)
            except asyncio.CancelledError:
                await run.queue.put({"type": "done", "reason": "cancelled"})
                raise
            except Exception as exc:  # ошибка ядра не должна оставлять сессию «занятой»
                log.exception("агент: сессия %s", session_id)
                await run.queue.put({"type": "error", "message": f"Внутренняя ошибка агента: {exc}"})
                await run.queue.put({"type": "done", "reason": "error"})
            finally:
                for fut in run.pending.values():
                    fut.cancel()
                service.runs.pop(session_id, None)
                await run.queue.put(None)

        run.task = asyncio.create_task(produce())

        async def stream():
            try:
                yield sse({"type": "session", "id": session_id, "model": session.model})
                while True:
                    try:
                        event = await asyncio.wait_for(run.queue.get(), HEARTBEAT)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if event is None:
                        break
                    yield sse(event)
            finally:
                # Клиент ушёл — запрос отменяется (история при этом остаётся корректной).
                if not run.task.done():
                    run.task.cancel()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache", "x-accel-buffering": "no"})

    @app.post("/agent/sessions/{session_id}/approve")
    async def approve(session_id: str, req: Approval, token: Token = Depends(authenticate)):
        service.profile(token, error_cls)
        session_or_404(session_id, token)
        if req.decision not in ("allow", "deny", "always"):
            raise error_cls(400, "decision: allow | deny | always", code="bad_decision")
        run = service.runs.get(session_id)
        fut = run.pending.get(req.id) if run else None
        if fut is None or fut.done():
            raise error_cls(404, "Нет ожидающего подтверждения с таким id", code="approval_not_found")
        fut.set_result(req.decision)
        return {"id": req.id, "decision": req.decision}

    @app.post("/agent/sessions/{session_id}/cancel")
    async def cancel(session_id: str, token: Token = Depends(authenticate)):
        service.profile(token, error_cls)
        session_or_404(session_id, token)
        run = service.runs.get(session_id)
        if run is None or run.task is None:
            return {"cancelled": False}
        run.task.cancel()
        return {"cancelled": True}

    @app.get("/agent/tools")
    async def tools(token: Token = Depends(authenticate)):
        profile = service.profile(token, error_cls)
        mcp = await service.mcp_for(token.agent_profile, profile)
        registry = make_registry(profile, mcp)
        return {"profile": token.agent_profile, "permissions_mode": profile["permissions"]["mode"],
                "data": [{"name": t.name, "description": t.description, "default_permission": t.default_permission}
                         for t in registry.tools.values()],
                "mcp": mcp.status() if mcp else []}
