"""Хуки: внешние команды на события агента.

Команда получает JSON события на stdin. Ответ:
  код 0, stdout пуст или не JSON      — продолжить (stdout — в журнал);
  код 0, stdout — JSON-объект          — {"decision": "allow|ask|deny", "reason", "updated_input", "additional_context"};
  код 2                                — блокировка, причина — stderr;
  другой код / таймаут / ошибка запуска — предупреждение, событие продолжается.
Подробности и примеры — docs/wiki/hooks.md.
"""

import asyncio
import json
import os
import re
import signal
from dataclasses import dataclass, field

from .settings import HOOK_EVENTS

RANK = {None: 0, "allow": 1, "ask": 2, "deny": 3}


@dataclass
class HookResult:
    decision: str | None = None
    reason: str = ""
    updated_input: dict | None = None
    additional_context: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.decision == "deny"

    @property
    def context(self) -> str:
        return "\n".join(self.additional_context)


class Hooks:
    def __init__(self, config: dict, *, cwd: str, session_id: str = "", env: dict | None = None):
        self.config, self.cwd, self.session_id = config or {}, cwd, session_id
        self.env = env if env is not None else dict(os.environ)

    def matching(self, event: str, tool_name: str | None) -> list[dict]:
        out = []
        for hook in self.config.get(event, []):
            matcher = hook.get("matcher") or ""
            if matcher and matcher != "*" and tool_name is not None:
                if not re.fullmatch(matcher, tool_name):
                    continue
            out.append(hook)
        return out

    async def run(self, event: str, payload: dict, tool_name: str | None = None) -> HookResult:
        assert event in HOOK_EVENTS, event
        result = HookResult()
        data = {"event": event, "session_id": self.session_id, "cwd": self.cwd, **payload}
        for hook in self.matching(event, tool_name):
            if result.updated_input is not None and "tool_input" in data:
                data["tool_input"] = result.updated_input
            await self._run_one(hook, data, result)
            if result.blocked:
                break
        return result

    async def _run_one(self, hook: dict, data: dict, result: HookResult) -> None:
        command = hook["command"]
        env = {**self.env, "GWAGENT_EVENT": data["event"], "GWAGENT_SESSION": self.session_id,
               "GWAGENT_CWD": self.cwd, "GWAGENT_REQUEST_ID": data.get("request_id", ""),
               "GWAGENT_SPAN_ID": data.get("span_id", "")}
        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", command, cwd=self.cwd, env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            result.warnings.append(f"хук {command!r} не запустился: {exc}")
            return
        async def collect(stream):
            buf = bytearray()
            while chunk := await stream.read(65536):
                if len(buf) < 65536:
                    buf.extend(chunk[:65536-len(buf)])
            return bytes(buf)
        readers = [asyncio.create_task(collect(proc.stdout)), asyncio.create_task(collect(proc.stderr))]
        async def communicate():
            try:
                proc.stdin.write(json.dumps(data, ensure_ascii=False).encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Хук вправе завершиться, не читая stdin.
            finally:
                proc.stdin.close()
            await proc.wait()
            return await asyncio.gather(*readers)
        try:
            out, err = await asyncio.wait_for(communicate(), hook.get("timeout", 30))
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            _kill(proc)
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            result.warnings.append(f"хук {command!r}: таймаут {hook.get('timeout', 30)} с")
            return
        finally:
            # Дети могут держать stdout открытым даже после выхода родителя.
            _kill(proc)
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        stdout, stderr = out.decode("utf-8", "replace").strip(), err.decode("utf-8", "replace").strip()
        if proc.returncode == 2:
            result.decision, result.reason = "deny", stderr or f"заблокировано хуком {command!r}"
            return
        if proc.returncode != 0:
            result.warnings.append(f"хук {command!r}: код {proc.returncode}: {stderr[:500]}")
            return
        try:
            answer = json.loads(stdout) if stdout.startswith("{") else None
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            return
        decision = answer.get("decision")
        if decision in ("allow", "ask", "deny") and RANK[decision] > RANK[result.decision]:
            result.decision = decision
            result.reason = str(answer.get("reason") or "")
        if isinstance(answer.get("updated_input"), dict):
            result.updated_input = answer["updated_input"]
        if answer.get("additional_context"):
            result.additional_context.append(str(answer["additional_context"]))


def _kill(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
