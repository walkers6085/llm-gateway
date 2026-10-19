"""Цикл агента: модель → вызовы инструментов → результаты → модель, пока она не ответит текстом.

`Agent.run(text)` — асинхронный генератор событий (словари с ключом "type"), их одинаково
потребляют CLI, SSE-эндпоинт сервера и тесты. Состав событий — docs/wiki/agent-loop.md.
"""

import asyncio
import platform
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path

from .hooks import Hooks
from .llm import LLMClient, LLMError
from .permissions import ALLOW, ASK, DENY, Permissions
from .store import Session, SessionStore
from .tools.base import Registry, ToolContext, ToolError, ToolResult, parse_arguments, truncate

# approver(запрос) → "allow" | "deny" | "always"
Approver = Callable[[dict], Awaitable[str]]
CANCELLED = "Отменено пользователем"


class Agent:
    def __init__(self, *, llm: LLMClient, registry: Registry, settings: dict, store: SessionStore,
                 session: Session, hooks: Hooks | None = None, approver: Approver | None = None,
                 on_usage: Callable[[str, int, tuple[int, int]], None] | None = None,
                 context_extra: dict | None = None):
        self.llm, self.registry, self.settings = llm, registry, settings
        self.store, self.session = store, session
        self.hooks = hooks or Hooks({}, cwd=session.workspace, session_id=session.id)
        self.approver = approver
        self.on_usage = on_usage  # (модель, http-статус, (prompt, completion)) — учёт расхода на сервере
        self.ctx = ToolContext(workspace=Path(session.workspace or Path.cwd()), settings=settings,
                               session_id=session.id, extra=context_extra or {})
        self.session_context: str | None = None  # additional_context хуков SessionStart

    @property
    def model(self) -> str:
        return self.session.model or self.settings.get("model") or ""

    def system_prompt(self) -> str:
        parts = [self.settings.get("system_prompt", "").strip(),
                 f"Рабочая папка: {self.session.workspace}\n"
                 f"Сегодня: {datetime.now().strftime('%Y-%m-%d')}\nОС: {platform.system()}"]
        if self.session_context:
            parts.append(self.session_context)
        return "\n\n".join(p for p in parts if p)

    async def _hook(self, event: str, payload: dict, tool_name: str | None = None):
        result = await self.hooks.run(event, payload, tool_name)
        return result, [{"type": "warning", "message": w} for w in result.warnings]

    async def run(self, text: str) -> AsyncIterator[dict]:
        if self.session_context is None:
            hook, warnings = await self._hook("SessionStart", {"model": self.model})
            for ev in warnings:
                yield ev
            self.session_context = hook.context

        hook, warnings = await self._hook("UserPromptSubmit", {"prompt": text})
        for ev in warnings:
            yield ev
        if hook.blocked:
            yield {"type": "error", "message": f"Запрос заблокирован хуком: {hook.reason}"}
            yield {"type": "done", "reason": "blocked"}
            return
        if hook.context:
            text = f"{text}\n\n<context>\n{hook.context}\n</context>"
        self._save({"role": "user", "content": text})
        if not self.session.title:
            self.session.title = text.strip().splitlines()[0][:80] if text.strip() else ""
            self.store.update(self.session.id, title=self.session.title)

        tools = self.registry.specs()
        max_steps = self.settings.get("max_steps", 25)
        for _step in range(max_steps):
            history = self.store.messages(self.session.id)
            messages = [{"role": "system", "content": self.system_prompt()}, *history]
            message, usage = None, (0, 0)
            try:
                async for ev in self.llm.stream(self.model, messages, tools or None,
                                                self.settings.get("temperature")):
                    if ev["type"] == "text":
                        yield ev
                    else:
                        message, usage = ev["message"], ev["usage"]
            except LLMError as exc:
                self._usage(exc.status, (0, 0))
                yield {"type": "error", "message": f"Ошибка модели ({exc.status}): {exc.message}"}
                yield {"type": "done", "reason": "error"}
                return
            self._usage(200, usage)
            yield {"type": "usage", "prompt_tokens": usage[0], "completion_tokens": usage[1]}
            self._save(message)
            calls = message.get("tool_calls") or []
            yield {"type": "assistant", "content": message.get("content"), "tool_calls": calls}

            if not calls:
                hook, warnings = await self._hook("Stop", {"last_message": message.get("content") or ""})
                for ev in warnings:
                    yield ev
                if hook.blocked and hook.reason:
                    # Хук Stop с deny = «не останавливайся»: причина уходит модели как новая реплика.
                    self._save({"role": "user", "content": hook.reason})
                    continue
                yield {"type": "done", "reason": "stop"}
                return

            done_ids: set[str] = set()
            try:
                for call in calls:
                    async for ev in self._execute(call):
                        yield ev
                    done_ids.add(call["id"])
            except asyncio.CancelledError:
                for call in calls:
                    if call["id"] not in done_ids:
                        self._save({"role": "tool", "tool_call_id": call["id"], "content": CANCELLED})
                raise

        yield {"type": "error", "message": f"Достигнут лимит шагов ({max_steps})"}
        yield {"type": "done", "reason": "max_steps"}

    async def _execute(self, call: dict) -> AsyncIterator[dict]:
        fn = call.get("function") or {}
        name, call_id = fn.get("name", ""), call["id"]
        started = time.monotonic()
        decision, args = "error", {}
        try:
            args = parse_arguments(fn.get("arguments"))
            tool = self.registry.get(name)
            if tool is None:
                raise ToolError(f"нет инструмента {name!r}; доступны: {', '.join(self.registry.names())}")
            tool.check_args(args)
            yield {"type": "tool_call", "id": call_id, "name": name, "arguments": args}

            hook, warnings = await self._hook("PreToolUse", {"tool_name": name, "tool_input": args}, name)
            for ev in warnings:
                yield ev
            if hook.updated_input is not None:
                args = hook.updated_input
                tool.check_args(args)
            perms = Permissions(self.settings.get("permissions", {}), self.session.allow_rules)
            verdict, reason = perms.decide(tool, args, hook.decision)
            if hook.decision and hook.reason:
                reason = hook.reason
            decision = verdict
            if verdict == ASK:
                request = {"type": "permission_request", "id": call_id, "name": name, "arguments": args,
                           "subject": tool.subject(args), "reason": reason, "rule": tool.always_rule(args)}
                yield request
                answer = await self._ask(request)
                if answer == "always":
                    rule = tool.always_rule(args)
                    if rule not in self.session.allow_rules:
                        self.session.allow_rules.append(rule)
                        self.store.update(self.session.id, allow_rules=self.session.allow_rules)
                verdict = ALLOW if answer in ("allow", "always") else DENY
                decision = f"ask:{verdict}"
                reason = "отклонено пользователем" if verdict == DENY else reason
            if verdict == DENY:
                result = ToolResult(f"Вызов отклонён: {reason}", is_error=True)
            else:
                out = await tool.run(args, self.ctx)
                result = out if isinstance(out, ToolResult) else ToolResult(str(out))
        except ToolError as exc:
            result = ToolResult(f"Ошибка: {exc}", is_error=True)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # инструмент упал — модель должна узнать, а цикл — продолжиться
            result = ToolResult(f"Ошибка инструмента: {type(exc).__name__}: {exc}", is_error=True)

        if decision != "error" and not decision.endswith(DENY):
            hook, warnings = await self._hook(
                "PostToolUse", {"tool_name": name, "tool_input": args, "tool_output": result.content,
                                "is_error": result.is_error}, name)
            for ev in warnings:
                yield ev
            if hook.blocked:
                result = ToolResult(f"Результат заблокирован хуком: {hook.reason}", is_error=True)
            elif hook.context:
                result.content += f"\n\n<context>\n{hook.context}\n</context>"

        content = truncate(result.content, self.settings.get("max_tool_output_chars", 30000))
        ms = int((time.monotonic() - started) * 1000)
        self._save({"role": "tool", "tool_call_id": call_id, "content": content})
        self.store.record_tool(self.session.id, call_id, name, args, decision, not result.is_error, ms)
        yield {"type": "tool_result", "id": call_id, "name": name, "ok": not result.is_error,
               "content": content, "decision": decision, "ms": ms}

    async def _ask(self, request: dict) -> str:
        if self.approver is None:
            return "deny"
        timeout = self.settings.get("permissions", {}).get("timeout", 600)
        try:
            answer = await asyncio.wait_for(self.approver(request), timeout)
        except asyncio.TimeoutError:
            return "deny"
        return answer if answer in ("allow", "deny", "always") else "deny"

    def _save(self, message: dict) -> None:
        self.store.add_message(self.session.id, message)

    def _usage(self, status: int, usage: tuple[int, int]) -> None:
        if self.on_usage is not None:
            self.on_usage(self.model, status, usage)
