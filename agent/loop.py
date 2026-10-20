"""Цикл агента: модель → вызовы инструментов → результаты → модель, пока она не ответит текстом.

`Agent.run(text)` — асинхронный генератор событий (словари с ключом "type"), их одинаково
потребляют CLI, SSE-эндпоинт сервера и тесты. Состав событий — docs/wiki/agent-loop.md.
"""

import asyncio
import platform
import json
import copy
import time
from contextlib import aclosing
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from .hooks import Hooks
from .credentials import sanitize, redact
from .llm import LLMClient, LLMError
from .tracing import Trace, current, identifier, record
from .tools.subagent import Subagent
from .tools.planning import Goal, Todo
from .permissions import ALLOW, ASK, DENY, Permissions
from .store import Session, SessionStore, task_title
from .checkpoints import capture
from .tools.base import Registry, ToolContext, ToolError, ToolResult, parse_arguments, truncate

# approver(запрос) → "allow" | "deny" | "always"
Approver = Callable[[dict], Awaitable[str]]
CANCELLED = "Отменено пользователем"


class Agent:
    def __init__(self, *, llm: LLMClient, registry: Registry, settings: dict, store: SessionStore,
                 session: Session, hooks: Hooks | None = None, approver: Approver | None = None,
                 on_usage: Callable[[str, int, tuple[int, int]], None] | None = None,
                 context_extra: dict | None = None, questioner=None):
        self.llm, self.registry, self.settings = llm, registry, copy.deepcopy(settings)
        self.store, self.session = store, session
        self.hooks = hooks or Hooks({}, cwd=session.workspace, session_id=session.id)
        self.approver = approver
        self.questioner = questioner
        self.active_mode = None
        self.running_request_id = None
        self.input_id = None
        self.segment_id = None
        self.on_usage = on_usage  # (модель, http-статус, (prompt, completion)) — учёт расхода на сервере
        self.ctx = ToolContext(workspace=Path(session.workspace or Path.cwd()), settings=settings,
                               session_id=session.id, extra=context_extra or {})
        self.ctx.extra['questioner'] = self._question
        self.ctx.extra['planning_state'] = lambda: self.store.planning_state(self.session.id)
        self.context_limit = None
        self.session_context: str | None = None  # additional_context хуков SessionStart

    @property
    def model(self) -> str:
        return self.session.model or self.settings.get("model") or ""

    @property
    def mode(self):
        return self.active_mode or self.session.agent_mode

    def tool_specs(self):
        return [tool.spec() for tool in self.registry.tools.values() if self.mode != 'plan' or tool.plan_safe]

    async def _question(self, request):
        if self.questioner is None:
            raise ToolError('интерактивные вопросы недоступны; задайте вопрос пользователю обычным ответом')
        trace = current.get()
        payload = {**request, 'request_id': trace.request_id, 'span_id': trace.span_id}
        record(self.store, 'question.start', question_id=request['id'])
        try:
            answer = await asyncio.wait_for(self.questioner(payload), self.settings.get('permissions', {}).get('timeout', 600))
            if answer is not None and (not isinstance(answer, str) or len(answer) > 16000):
                raise ToolError('ответ должен быть текстом до 16000 символов')
            record(self.store, 'question.end', question_id=request['id'], answered=answer is not None)
            return answer
        except TimeoutError:
            record(self.store, 'question.end', question_id=request['id'], answered=False, timeout=True)
            return None
        except asyncio.CancelledError:
            record(self.store, 'question.end', question_id=request['id'], answered=False, cancelled=True)
            raise
        except Exception:
            record(self.store, 'question.end', question_id=request['id'], answered=False, error=True)
            raise

    def system_prompt(self) -> str:
        parts = [self.settings.get("system_prompt", "").strip(),
                 f"Рабочая папка: {self.session.workspace}\n"
                 f"Сегодня: {datetime.now().strftime('%Y-%m-%d')}\nОС: {platform.system()}"]
        parts.append('Активный агент: ' + self.mode + '. ' + (
            'Исследуй проект и уточняй требования. Веди goal и todo, задавай вопросы через question. '
            'Составь план; изменения файлов, shell, произвольные HTTP-запросы и MCP в этом режиме недоступны.'
            if self.mode == 'plan' else 'Выполняй согласованную задачу и план, обновляй статусы задач.'))
        if self.session.summary:
            parts.append(self.session.summary)
        if self.session_context:
            parts.append(self.session_context)
        return redact("\n\n".join(p for p in parts if p))

    async def _hook(self, event: str, payload: dict, tool_name: str | None = None):
        trace = current.get()
        if trace:
            payload = {**payload, "request_id": trace.request_id, "span_id": trace.span_id}
        record(self.store, "hook.start", hook=event, tool=tool_name)
        result = await self.hooks.run(event, payload, tool_name)
        record(self.store, "hook.end", hook=event, blocked=result.blocked, warnings=len(result.warnings))
        return result, [{"type": "warning", "message": w} for w in result.warnings]

    async def run(self, text: str, *, request_id=None, delegate=False) -> AsyncIterator[dict]:
        self.active_mode = self.session.agent_mode
        turn = self.store.begin_turn(self.session.id)
        parent = current.get()
        trace = Trace(parent.request_id if parent else request_id or identifier(), turn, self.session.id,
                      parent.span_id if parent else "", parent.budget if parent else {"spent": 0, "children": 0, "started": time.monotonic()})
        token = current.set(trace)
        self.running_request_id = trace.request_id
        self.input_id = self.segment_id = trace.request_id
        state, started = 'interrupted', time.monotonic()
        try:
            record(self.store, "request.start", model=self.model, connection_id=self.session.connection_id, delegated=bool(parent), agent_mode=self.mode)
            if parent is None:
                self._checkpoint(trace.request_id, before=True)
            async with aclosing(self._run(text, delegate=delegate)) as events:
                async for event in events:
                    if event['type'] == 'done':
                        state = event['reason']
                    yield sanitize({"request_id": trace.request_id, "span_id": trace.span_id, "parent_span_id": trace.parent_span_id, "agent_mode": self.mode, **event})
        except Exception:
            state = "error"
            raise
        finally:
            try:
                self.store.repair_calls(self.session.id, CANCELLED)
                if parent is None:
                    self._checkpoint(self.segment_id, before=False)
                self.store.end_turn(turn, state)
                record(self.store, "request.end", state=state, ms=int((time.monotonic()-started)*1000))
            finally:
                current.reset(token)
                self.active_mode = None
                self.running_request_id = None

    def _checkpoint(self, request_id, *, before):
        excluded = [self.store.path + suffix for suffix in ('', '-wal', '-shm')]
        try:
            data = capture(self.session.workspace or Path.cwd(), excluded)
            self.store.checkpoint(self.session.id, request_id, **({'before': data} if before else {'after': data}))
        except (OSError, ValueError) as exc:
            self.store.checkpoint(self.session.id, request_id, error=str(exc))

    async def _model_events(self, messages, tools):
        parent = current.get()
        trace = Trace(parent.request_id, identifier(), self.session.id, parent.span_id, parent.budget)
        token = current.set(trace)
        status, usage, started = "interrupted", (0, 0), time.monotonic()
        try:
            record(self.store, "model.start", model=self.model, connection_id=self.session.connection_id, input_id=self.input_id)
            async with aclosing(self.llm.stream(self.model, messages, tools or None,
                                               self.settings.get("temperature"))) as events:
                async for event in events:
                    if event["type"] == "message":
                        usage, status = event["usage"], 200
                        elapsed = max(0, time.monotonic() - started)
                        event = {**event, "metrics": {"duration_ms": round(elapsed * 1000), "worked_ms": round((time.monotonic() - parent.budget["started"]) * 1000),
                            "completed_at": datetime.now(timezone.utc).isoformat(), "completion_tokens": usage[1],
                            "tokens_per_second": round(usage[1] / elapsed, 2) if usage[1] and elapsed > 0 else None}}
                    yield {**event, "span_id": trace.span_id, "parent_span_id": trace.parent_span_id}
        except LLMError as exc:
            status = exc.status
            raise
        except Exception:
            status = "error"
            raise
        finally:
            try:
                record(self.store, "model.end", model=self.model, status=status, prompt_tokens=usage[0],
                       completion_tokens=usage[1], ms=int((time.monotonic()-started)*1000))
            finally:
                current.reset(token)

    def _context_messages(self, limit):
        self.session = self.store.get(self.session.id)
        history = self.store.context_messages(self.session.id)
        # Исходная цель остаётся рядом с последним запросом после любой контрольной точки.
        timeline = self.store.timeline(self.session.id)
        all_users = [row['message'] for row in timeline if row['message']['role'] == 'user']
        users = [row['message'] for row in timeline if row['message']['role'] == 'user' and 'user_input' in row['metadata']]
        if not users:
            users = all_users
        original = all_users[0] if all_users else None
        has_plan = any('planning' in row['metadata'] for row in timeline)
        if users and users[-1] not in history:
            history.insert(0, users[-1])
        if not has_plan and self.session.context_seq and original and original != next((m for m in history if m['role'] == 'user'), None):
            goal = original.get('content') or ''
            size = max(128, min(2000, limit // 16))
            if len(goal) > size:
                goal = goal[:size] + '\n[Выдержка исходной цели; полная версия сохранена в истории]'
            history.insert(0, {"role": "user", "content": "Исходная цель сессии:\n" + goal})
        plan = self.store.planning_state(self.session.id)
        if has_plan:
            goal_size = max(64, min(2000, limit // 32))
            text_size = max(16, min(200, limit // (32 * (len(plan['todos']) + 1))))
            compact_plan = {'goal': plan['goal'][:goal_size], 'todos': [
                {**item, 'text': item['text'][:text_size]} for item in plan['todos']]}
            history.insert(0, {'role': 'user', 'content': 'Текущая цель и план (рабочие данные):\n' + json.dumps(compact_plan, ensure_ascii=False)})
        return [{"role": "system", "content": self.system_prompt()}, *history]

    def context_status(self):
        limits = {**self.settings, **self.settings.get('model_profiles', {}).get(self.model, {})}
        limit = self.context_limit or limits.get('max_input_tokens', 32768)
        messages = self._context_messages(limit)
        estimated = len(json.dumps([messages, self.tool_specs()], ensure_ascii=False).encode()) + 64
        return {'estimated_tokens': estimated, 'max_input_tokens': limit,
                'max_output_tokens': limits.get('max_output_tokens', 1024), 'source': 'estimate'}

    def _context(self, tools, limit, *, force=False):
        """Сжатие между шагами; исходная история и выполненные действия не меняются."""
        changed = False
        summary_chars = max(0, min(4000, limit // 16))
        if force:
            changed = self.store.compact(self.session.id, max_chars=summary_chars)
        while True:
            messages = self._context_messages(limit)
            estimated = len(json.dumps([messages, tools], ensure_ascii=False).encode()) + 64
            if estimated <= limit and not force:
                return messages, estimated, changed
            if self.store.compact(self.session.id, max_chars=summary_chars):
                changed = True
                continue
            # Большой результат последнего инструмента сокращаем только в запросе модели.
            # Сохраняем id, аргументы, все результаты группы и пользовательский текст.
            for size in (4096, 1024, 256, 64):
                candidate = copy.deepcopy(messages)
                for message in candidate:
                    if message['role'] == 'tool' and len((message.get('content') or '').encode()) > size:
                        message['content'] = message['content'].encode()[:size].decode('utf-8', 'ignore') + '\n[Результат сокращён для контекста; полный текст сохранён в истории]'
                estimate = len(json.dumps([candidate, tools], ensure_ascii=False).encode()) + 64
                if estimate <= limit:
                    return candidate, estimate, True
            return messages, estimated, changed

    @staticmethod
    def _context_error(exc):
        text = exc.message.lower()
        return exc.status in (400, 413, 422, 502) and any(marker in text for marker in (
            'context_length_exceeded', 'context window', 'context length', 'maximum context',
            'too many tokens', 'max context', 'input tokens exceeds', 'context limit',
            'prompt is too long', 'input is too long', '上下文',
            'окно контекста', 'лимит контекста', 'длина контекста', 'размер контекста'))

    def steer(self, text, *, input_id=None):
        if not self.running_request_id:
            raise ValueError('нет активного цикла для уточнения')
        item = self.store.enqueue_input(self.session.id, self.running_request_id, input_id or identifier(), redact(text))
        self.store.trace(self.running_request_id, self.running_request_id, '', self.session.id,
                         'input.queued', {'input_id': item['id']})
        return item

    async def _drain_inputs(self, *, recovered=False):
        trace = current.get()
        pending = self.store.pending_inputs(self.session.id)
        for item in pending:
            if recovered != (item['request_id'] != trace.request_id):
                continue
            hook, warnings = await self._hook('UserPromptSubmit', {'prompt': item['text'], 'input_id': item['id'], 'steering': True})
            for event in warnings:
                yield event
            if hook.blocked:
                self.store.reject_input(item['id'])
                record(self.store, 'input.rejected', input_id=item['id'])
                yield {"type": "input_rejected", "id": item['id'], "message": 'Уточнение заблокировано хуком: ' + hook.reason}
                continue
            if not recovered and not trace.parent_span_id:
                self._checkpoint(self.segment_id, before=False)
                self.segment_id = item['id']
                self._checkpoint(self.segment_id, before=True)
            self.input_id = item['id']
            text = item['text'] + ('\n\n<context>\n' + hook.context + '\n</context>' if hook.context else '')
            self._save({'role': 'user', 'content': text}, user_input=item['text'], steering={**item, 'recovered': recovered})
            record(self.store, 'input.applied', input_id=item['id'], source_request_id=item['request_id'], recovered=recovered)
            yield {"type": "input_applied", "id": item['id'], "text": item['text'], "recovered": recovered}

    async def _run(self, text: str, *, delegate=False) -> AsyncIterator[dict]:
        if self.session_context is None:
            hook, warnings = await self._hook("SessionStart", {"model": self.model})
            for ev in warnings:
                yield ev
            self.session_context = hook.context

        async for event in self._drain_inputs(recovered=True):
            yield event
        self.input_id = current.get().request_id
        user_text = text
        hook, warnings = await self._hook("UserPromptSubmit", {"prompt": text})
        for ev in warnings:
            yield ev
        if hook.blocked:
            yield {"type": "error", "message": f"Запрос заблокирован хуком: {hook.reason}"}
            yield {"type": "done", "reason": "blocked"}
            return
        if hook.context:
            text = f"{text}\n\n<context>\n{hook.context}\n</context>"
        self._save({"role": "user", "content": text}, user_input=user_text)
        if not self.session.title or self.session.title_auto and self.session.title.startswith('Новая сессия ·'):
            self.session.title = task_title(user_text)
            self.store.update(self.session.id, title=self.session.title)

        if delegate:
            call = {"id": identifier(), "type": "function", "function": {
                "name": "subagent", "arguments": json.dumps({"task": text}, ensure_ascii=False)}}
            self._save({"role": "assistant", "content": None, "tool_calls": [call]})
            ok = False
            async with aclosing(self._execute(call)) as events:
                async for event in events:
                    if event["type"] == "tool_result":
                        ok = event["ok"]
                    yield event
            previous_input = self.input_id
            async for event in self._drain_inputs():
                yield event
            if self.input_id == previous_input:
                yield {"type": "done", "reason": "stop" if ok else "error"}
                return

        tools = self.tool_specs()
        max_steps = self.settings.get("max_steps", 25)
        budget = current.get().budget
        limits = {**self.settings, **self.settings.get('model_profiles', {}).get(self.model, {})}
        if isinstance(self.llm, LLMClient):
            self.llm.max_tokens = limits.get('max_output_tokens', 1024)
            self.llm.options = {**self.llm.base_options, **limits.get('options', {})}
        effective_input_limit = limits.get('max_input_tokens', 32768)
        self.context_limit = effective_input_limit
        for _step in range(max_steps):
            async for event in self._drain_inputs():
                yield event
            reserve = limits.get('max_output_tokens', 1024)
            input_limit = effective_input_limit
            messages, estimated, compacted = self._context(tools, input_limit)
            if compacted:
                record(self.store, "context.compacted", estimated=estimated, automatic=True)
                yield {"type": "warning", "message": "Контекст автоматически сокращён; цель и последний запрос сохранены"}
            message, usage, metrics = None, (0, 0), None
            for attempt in range(2):
                if estimated > input_limit or budget["spent"] + estimated + reserve > limits.get('max_turn_tokens', 65536):
                    reason = "Контекст не помещается после сжатия: сократите запрос или увеличьте max_input_tokens" if estimated > input_limit else "Исчерпан общий бюджет хода max_turn_tokens; сжатие не обнуляет расход"
                    yield {"type": "error", "message": reason}
                    yield {"type": "done", "reason": "budget"}
                    return
                yield {"type": "context", "estimated_tokens": estimated, "max_input_tokens": input_limit,
                       "max_output_tokens": reserve, "source": "estimate",
                       "planning": self.store.planning_state(self.session.id)}
                streamed = False
                try:
                    async for ev in self._model_events(messages, tools):
                        if ev["type"] in ("text", "reasoning"):
                            streamed = True
                            yield ev
                        elif ev["type"] == "message":
                            message, usage, metrics = ev["message"], ev["usage"], ev.get("metrics")
                    break
                except LLMError as exc:
                    self._usage(exc.status, (0, 0))
                    if not attempt and not streamed and self._context_error(exc):
                        target = min(input_limit, max(1, estimated // 2))
                        reduced, size, changed = self._context(tools, target, force=True)
                        if size < estimated and size <= target:
                            messages, estimated = reduced, size
                            effective_input_limit = target
                            self.context_limit = target
                            record(self.store, "context.compacted", estimated=size, automatic=True, retry=True)
                            yield {"type": "warning", "message": "Провайдер сообщил о переполнении контекста; повторяю запрос после сжатия"}
                            continue
                    yield {"type": "error", "message": f"Ошибка модели ({exc.status}): {exc.message}"}
                    yield {"type": "done", "reason": "error"}
                    return
            budget["spent"] += sum(usage) if any(usage) else estimated + reserve
            self._usage(200, usage)
            yield {"type": "usage", "prompt_tokens": usage[0], "completion_tokens": usage[1]}
            self._save(message, metrics=metrics)
            calls = message.get("tool_calls") or []
            yield {"type": "assistant", "content": message.get("content"), "tool_calls": calls, "reasoning_content": message.get("reasoning_content", ""), "metrics": metrics}

            if not calls:
                previous_input = self.input_id
                async for event in self._drain_inputs():
                    yield event
                if self.input_id != previous_input:
                    continue
                hook, warnings = await self._hook("Stop", {"last_message": message.get("content") or ""})
                for ev in warnings:
                    yield ev
                if hook.blocked and hook.reason:
                    # Хук Stop с deny = «не останавливайся»: причина уходит модели как новая реплика.
                    self._save({"role": "user", "content": hook.reason})
                    continue
                await asyncio.sleep(0)
                previous_input = self.input_id
                async for event in self._drain_inputs():
                    yield event
                if self.input_id != previous_input:
                    continue
                yield {"type": "done", "reason": "stop"}
                return

            done_ids: set[str] = set()
            try:
                for call in calls:
                    pending = self.store.pending_inputs(self.session.id)
                    if pending:
                        content = 'Не выполнено: поступило уточнение пользователя; пересмотри действие с учётом нового запроса'
                        self._save({'role': 'tool', 'tool_call_id': call['id'], 'content': content})
                        name = call.get('function', {}).get('name', '')
                        record(self.store, 'tool.skipped', call_id=call['id'], tool=name, input_ids=[item['id'] for item in pending])
                        self.store.record_tool(self.session.id, call['id'], name, {}, 'steering:skip', False, 0,
                                               request_id=current.get().request_id, span_id=current.get().span_id)
                        yield {"type": "tool_result", "id": call['id'], "name": name, "ok": False, "content": content, "decision": "steering:skip", "ms": 0}
                        continue
                    async with aclosing(self._execute(call)) as execution:
                        async for ev in execution:
                            yield ev
                    done_ids.add(call["id"])
            except asyncio.CancelledError:
                self.store.repair_calls(self.session.id, CANCELLED)
                raise

        yield {"type": "error", "message": f"Достигнут лимит шагов ({max_steps})"}
        yield {"type": "done", "reason": "max_steps"}

    async def _execute(self, call):
        parent = current.get()
        token = current.set(Trace(parent.request_id, identifier(), self.session.id, parent.span_id, parent.budget))
        started, state = time.monotonic(), "interrupted"
        try:
            record(self.store, "tool.start", tool=call.get("function", {}).get("name", ""), call_id=call["id"])
            async with aclosing(self._execute_inner(call)) as events:
                async for event in events:
                    if event["type"] == "tool_result":
                        state = "ok" if event["ok"] else "error"
                        record(self.store, "tool.result", call_id=call["id"], decision=event["decision"], ok=event["ok"])
                    yield {"span_id": current.get().span_id, "parent_span_id": current.get().parent_span_id, **event}
        finally:
            try:
                record(self.store, "tool.end", tool=call.get("function", {}).get("name", ""), call_id=call["id"],
                       state=state, ms=int((time.monotonic()-started)*1000))
            finally:
                current.reset(token)

    async def _delegate(self, args):
        trace = current.get()
        config = self.settings.get("subagents", {})
        if not config.get("enabled", True) or not self.registry.get("subagent"):
            raise ToolError("сабагенты отключены")
        if trace.budget["children"] >= config.get("max_calls", 3):
            raise ToolError("достигнут лимит сабагентов для запроса")
        trace.budget["children"] += 1
        settings = copy.deepcopy(self.settings)
        settings["max_steps"] = min(settings["max_steps"], config.get("max_steps", 8))
        settings["subagents"] = {**config, "enabled": False}
        session = self.store.create(self.session.owner, title="Сабагент: " + args["task"][:60],
                                    model=self.model, profile=self.session.profile, workspace=self.session.workspace,
                                    connection_id=self.session.connection_id, endpoint_hash=self.session.endpoint_hash, agent_mode=self.mode)
        session.allow_rules = list(self.session.allow_rules)
        self.store.update(session.id, allow_rules=session.allow_rules)
        llm = copy.copy(self.llm)
        registry = Registry([tool for tool in self.registry.tools.values() if not isinstance(tool, Subagent)])
        child = Agent(llm=llm, registry=registry, settings=settings, store=self.store, session=session,
                      approver=self.approver, on_usage=self.on_usage, questioner=self.questioner,
                      hooks=Hooks(settings["hooks"], cwd=session.workspace, session_id=session.id, env=self.hooks.env))
        result, reason = [], "interrupted"
        try:
            record(self.store, "subagent.start", child_session_id=session.id)
            yield {"type": "subagent_start", "session_id": session.id, "task": args["task"]}
            async with asyncio.timeout(config.get("timeout", 120)):
                async with aclosing(child.run(args["task"])) as events:
                    async for event in events:
                        if event["type"] == "assistant" and event.get("content"):
                            result.append(event["content"])
                        if event["type"] == "done":
                            reason = event["reason"]
                        if event["type"] in ("permission_request", "usage"):
                            yield event
                        else:
                            yield {"type": "subagent_event", "session_id": session.id, "event": event}
        except TimeoutError:
            reason = "timeout"
        finally:
            record(self.store, "subagent.end", child_session_id=session.id, state=reason)
        yield {"type": "subagent_end", "session_id": session.id, "reason": reason, "content": "\n\n".join(result)}
        yield {"type": "subagent_result", "result": ToolResult(
            "\n\n".join(result) or f"Сабагент завершён: {reason}", is_error=reason != "stop")}

    async def _execute_inner(self, call: dict) -> AsyncIterator[dict]:
        fn = call.get("function") or {}
        name, call_id = fn.get("name", ""), call["id"]
        started = time.monotonic()
        decision, args = "error", {}
        try:
            args = parse_arguments(fn.get("arguments"))
            tool = self.registry.get(name)
            if tool is None:
                raise ToolError(f"нет инструмента {name!r}; доступны: {', '.join(self.registry.names())}")
            if self.mode == 'plan' and not tool.plan_safe:
                raise ToolError(f'инструмент {name} недоступен агенту plan; переключитесь на build')
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
                if isinstance(tool, Subagent):
                    out = ToolResult("Сабагент прерван", is_error=True)
                    async with aclosing(self._delegate(args)) as events:
                        async for event in events:
                            if event["type"] == "subagent_result":
                                out = event["result"]
                            else:
                                yield event
                else:
                    trace = current.get()
                    self.ctx.extra.update(request_id=trace.request_id, span_id=trace.span_id)
                    out = await tool.run(args, self.ctx)
                result = out if isinstance(out, ToolResult) else ToolResult(str(out))
        except ToolError as exc:
            result = ToolResult(f"Ошибка: {exc}", is_error=True)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # инструмент упал — модель должна узнать, а цикл — продолжиться
            result = ToolResult(f"Ошибка инструмента: {type(exc).__name__}: {exc}", is_error=True)

        planning = None
        if not result.is_error and isinstance(tool, (Goal, Todo)):
            planning = json.loads(result.content)

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

        content = truncate(redact(result.content), self.settings.get("max_tool_output_chars", 30000))
        ms = int((time.monotonic() - started) * 1000)
        if result.is_error:
            planning = None
        self._save({"role": "tool", "tool_call_id": call_id, "content": content}, planning=planning)
        self.store.record_tool(self.session.id, call_id, name, sanitize(args), decision, not result.is_error, ms,
                               request_id=current.get().request_id, span_id=current.get().span_id)
        yield {"type": "tool_result", "id": call_id, "name": name, "ok": not result.is_error,
               "content": content, "decision": decision, "ms": ms}

    async def _ask(self, request: dict) -> str:
        if self.approver is None:
            return "deny"
        trace = current.get()
        if trace:
            request = {**request, "request_id": trace.request_id, "span_id": trace.span_id}
        timeout = self.settings.get("permissions", {}).get("timeout", 600)
        try:
            answer = await asyncio.wait_for(self.approver(request), timeout)
        except asyncio.TimeoutError:
            return "deny"
        return answer if answer in ("allow", "deny", "always") else "deny"

    def _save(self, message: dict, *, metrics=None, user_input=None, planning=None, steering=None) -> None:
        trace = current.get()
        metadata = {"request_id": trace.request_id, "span_id": trace.span_id, "agent_mode": self.mode}
        metadata.update(input_id=self.input_id, checkpoint_id=self.segment_id)
        if steering:
            metadata.update(steering=True, input_id=steering['id'], source_request_id=steering['request_id'],
                            checkpoint_id=self.segment_id if steering.get('recovered') else steering['id'])
        if planning is not None:
            metadata['planning'] = planning
        if metrics:
            metadata['metrics'] = metrics
        if user_input is not None:
            metadata['user_input'] = user_input
        seq = self.store.add_message(self.session.id, sanitize(message), sanitize(metadata))
        record(self.store, "message.saved", role=message["role"], seq=seq)

    def _usage(self, status: int, usage: tuple[int, int]) -> None:
        if self.on_usage is not None:
            self.on_usage(self.model, status, usage)
