"""Инструмент shell: команда в рабочей папке с таймаутом и ограничением вывода.

Это НЕ песочница: команда работает с правами процесса агента. Защита — права
(по умолчанию каждый вызов требует подтверждения), урезанное окружение и таймаут.
Подробности — docs/wiki/tools/shell.md.
"""

import asyncio
import os
import signal

from .base import Tool, ToolContext, ToolResult, truncate


class Shell(Tool):
    name = "shell"
    description = ("Выполнить команду оболочки в рабочей папке. Возвращает код выхода и вывод "
                   "(stdout и stderr вместе). Интерактивные команды не работают; долгие — ограничены timeout.")
    parameters = {"type": "object", "properties": {
        "command": {"type": "string", "minLength": 1},
        "timeout": {"type": "integer", "minimum": 1, "description": "секунд, по умолчанию из настроек"},
    }, "required": ["command"], "additionalProperties": False}

    def subject(self, args: dict) -> str | None:
        return args.get("command", "").strip()

    async def run(self, args: dict, ctx: ToolContext):
        cfg = ctx.settings["shell"]
        timeout = min(args.get("timeout") or cfg["timeout"], cfg["max_timeout"])
        env = {k: os.environ[k] for k in cfg["env_passthrough"] if k in os.environ}
        env.update(cfg.get("env", {}))
        env["GWAGENT_SESSION"] = ctx.session_id
        proc = await asyncio.create_subprocess_exec(
            cfg["executable"], "-c", args["command"], cwd=str(ctx.workspace), env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)  # своя группа процессов — чтобы убить и детей
        limit = ctx.settings.get("max_tool_output_chars", 30000)
        buf = bytearray()
        reader = asyncio.create_task(_collect(proc.stdout, buf, limit * 8))
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            _killpg(proc)
            await proc.wait()
            await _finish(reader)
            text = truncate(buf.decode("utf-8", "replace"), limit)
            return ToolResult(f"Таймаут {timeout} с, процесс остановлен.\n{text}".rstrip(), is_error=True)
        except asyncio.CancelledError:
            _killpg(proc)
            reader.cancel()
            raise
        await _finish(reader)
        text = truncate(buf.decode("utf-8", "replace"), limit)
        code = proc.returncode
        return ToolResult(f"exit code: {code}\n{text}".rstrip(), is_error=code != 0)


async def _collect(stream, buf: bytearray, cap: int) -> None:
    """Читает вывод до EOF; больше `cap` байт не копит (остальное просто вычитывается)."""
    while chunk := await stream.read(65536):
        if len(buf) < cap:
            buf.extend(chunk[:cap - len(buf)])


async def _finish(reader: asyncio.Task) -> None:
    # Фоновый потомок мог унаследовать stdout и держать его открытым — не ждём его вечно.
    try:
        await asyncio.wait_for(reader, 2)
    except asyncio.TimeoutError:
        pass


def _killpg(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


TOOLS = [Shell]
