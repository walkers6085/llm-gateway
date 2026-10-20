"""Клиент MCP (Model Context Protocol) без сторонних библиотек: JSON-RPC 2.0 поверх
stdio (подпроцесс, одно сообщение — одна строка) или Streamable HTTP (POST, ответ JSON или SSE).

Инструменты серверов регистрируются у агента как `mcp__<сервер>__<инструмент>`.
Подробности — docs/wiki/mcp.md.
"""

import asyncio
import json
import logging
import os
import re
import signal
from collections import deque

import httpx

from .streaming import data_events
from .tools.base import Tool, ToolContext, ToolError, ToolResult

log = logging.getLogger("gwagent.mcp")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "gwagent", "version": "0.1"}
LINE_LIMIT = 16 * 1024 * 1024


class MCPError(Exception):
    pass


def subst(value):
    """`${VAR}` / `$VAR` из окружения в строках (рекурсивно в списках и словарях)."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [subst(v) for v in value]
    if isinstance(value, dict):
        return {k: subst(v) for k, v in value.items()}
    return value


def _rpc_error(err: dict) -> MCPError:
    return MCPError(f"{err.get('message', 'ошибка')} (код {err.get('code')})")


class StdioTransport:
    def __init__(self, name: str, cfg: dict):
        self.name, self.cfg = name, cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.stderr: deque[str] = deque(maxlen=20)
        self.next_id = 0
        self.read_failed = False
        self.tasks: list[asyncio.Task] = []

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and not self.read_failed

    async def start(self) -> None:
        env = {**os.environ, **subst(self.cfg.get("env", {}))}
        try:
            self.proc = await asyncio.create_subprocess_exec(
                subst(self.cfg["command"]), *subst(self.cfg.get("args", [])),
                cwd=subst(self.cfg.get("cwd") or None), env=env, limit=LINE_LIMIT, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            raise MCPError(f"не удалось запустить {self.cfg['command']!r}: {exc}") from None
        self.tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._read_stderr())]

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while line := await self.proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.debug("mcp %s: не JSON в stdout: %r", self.name, line[:200])
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" in msg:
                    if "id" in msg:  # запрос от сервера: отвечаем на ping, остальное не поддерживаем
                        if msg["method"] == "ping":
                            await self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
                        else:
                            await self._write({"jsonrpc": "2.0", "id": msg["id"],
                                               "error": {"code": -32601, "message": "not supported"}})
                    continue
                fut = self.pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    if msg.get('jsonrpc') != '2.0':
                        fut.set_exception(MCPError('неверная версия JSON-RPC'))
                    else:
                        fut.set_result(msg)
        except (ValueError, ConnectionError) as exc:  # слишком длинная строка и т. п.
            log.warning("mcp %s: чтение прервано: %s", self.name, exc)
        finally:
            self.read_failed = True
            tail = " | ".join(self.stderr)
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(MCPError(f"сервер {self.name} завершился" + (f": {tail}" if tail else "")))
            self.pending.clear()

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while line := await self.proc.stderr.readline():
            self.stderr.append(line.decode("utf-8", "replace").rstrip()[:300])

    async def _write(self, msg: dict) -> None:
        if not self.alive or not self.proc or not self.proc.stdin:
            raise MCPError(f"сервер {self.name} не запущен")
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False).encode() + b"\n")
        try:
            await self.proc.stdin.drain()
        except ConnectionError as exc:
            raise MCPError(f"сервер {self.name} закрыл stdin: {exc}") from None

    async def request(self, method: str, params: dict | None, timeout: float) -> dict:
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            msg = await asyncio.wait_for(fut, timeout)
            if "error" in msg:
                raise _rpc_error(msg["error"])
            return msg.get("result") or {}
        except asyncio.TimeoutError:
            raise MCPError(f"{self.name}: {method} не ответил за {timeout} с") from None
        finally:
            self.pending.pop(rid, None)
            if not fut.done():
                fut.cancel()

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 2)
            except (asyncio.TimeoutError, ConnectionError):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                await self.proc.wait()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class HttpTransport:
    def __init__(self, name: str, cfg: dict, proxy: str = ""):
        self.name, self.url = name, subst(cfg["url"])
        self.headers = subst(cfg.get("headers", {}))
        self.client = httpx.AsyncClient(trust_env=False, proxy=proxy or None, timeout=None)
        self.session_id: str | None = None
        self.protocol: str | None = None
        self.next_id = 0
        self.alive = True

    async def start(self) -> None:
        pass

    def _headers(self) -> dict:
        h = {"content-type": "application/json", "accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        if self.protocol:
            h["mcp-protocol-version"] = self.protocol
        return h

    async def _post(self, msg: dict, timeout: float) -> dict | None:
        try:
            req = self.client.build_request("POST", self.url, json=msg, headers=self._headers(),
                                            timeout=httpx.Timeout(timeout))
            resp = await self.client.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise MCPError(f"{self.name}: {type(exc).__name__}: {exc}") from None
        try:
            if resp.headers.get("mcp-session-id"):
                self.session_id = resp.headers["mcp-session-id"]
            if resp.status_code == 202:
                return None
            if resp.status_code == 404 and self.session_id:
                raise MCPError(f"{self.name}: сессия MCP истекла (404)")
            if resp.status_code >= 400:
                raise MCPError(f"{self.name}: HTTP {resp.status_code}")
            if resp.headers.get("content-type", "").startswith("text/event-stream"):
                return await asyncio.wait_for(self._from_sse(resp, msg["id"]), timeout)
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                if len(body) + len(chunk) > LINE_LIMIT:
                    raise MCPError('слишком большой ответ MCP')
                body.extend(chunk)
            if 'id' not in msg:
                return None
            result = json.loads(body)
            if not isinstance(result, dict) or result.get('id') != msg['id'] or result.get('jsonrpc') != '2.0':
                raise MCPError('неверный JSON-RPC id/version')
            return result
        except asyncio.TimeoutError:
            raise MCPError(f"{self.name}: {msg.get('method')} не ответил за {timeout} с") from None
        except (ValueError, httpx.HTTPError) as exc:
            raise MCPError(f"{self.name}: неверный ответ: {exc}") from None
        finally:
            await resp.aclose()

    @staticmethod
    async def _from_sse(resp: httpx.Response, rid: int) -> dict:
        async for payload in data_events(resp, LINE_LIMIT):
            msg = json.loads(payload)
            if isinstance(msg, dict) and msg.get('id') == rid and 'method' not in msg:
                if msg.get('jsonrpc') != '2.0':
                    raise MCPError('неверная версия JSON-RPC')
                return msg
        raise MCPError("поток закрыт без ответа")

    async def request(self, method: str, params: dict | None, timeout: float) -> dict:
        self.next_id += 1
        try:
            async with asyncio.timeout(timeout):
                msg = await self._post({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}, timeout)
        except TimeoutError:
            raise MCPError(f'{self.name}: общий таймаут {method}') from None
        if msg is None:
            raise MCPError(f"{self.name}: пустой ответ на {method}")
        if "error" in msg:
            raise _rpc_error(msg["error"])
        return msg.get("result") or {}

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}, 30)

    async def close(self) -> None:
        if self.session_id:
            try:
                await self.client.delete(self.url, headers=self._headers(), timeout=5)
            except httpx.HTTPError:
                pass
        await self.client.aclose()


class MCPClient:
    def __init__(self, name: str, cfg: dict, timeout: float = 60, proxy: str = ""):
        self.name, self.cfg = name, cfg
        self.timeout = cfg.get("timeout", timeout)
        self.proxy = proxy
        self.transport = self._make()
        self.server_info: dict = {}
        self.last_error = ""
        self.reconnect_lock = asyncio.Lock()

    def _make(self):
        if self.cfg["type"] == "stdio":
            if not self.cfg.get("command"):
                raise MCPError(f"{self.name}: для stdio нужен command")
            return StdioTransport(self.name, self.cfg)
        if not self.cfg.get("url"):
            raise MCPError(f"{self.name}: для http нужен url")
        return HttpTransport(self.name, self.cfg, self.proxy)

    async def connect(self) -> None:
        await self.transport.start()
        result = await self.transport.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO}, self.timeout)
        if result.get('protocolVersion') not in (PROTOCOL_VERSION, '2025-03-26', '2024-11-05'):
            raise MCPError('неподдерживаемая версия MCP')
        self.server_info = result.get("serverInfo") or {}
        if isinstance(self.transport, HttpTransport):
            self.transport.protocol = result.get("protocolVersion") or PROTOCOL_VERSION
        await self.transport.notify("notifications/initialized")

    async def reconnect(self) -> None:
        await self.transport.close()
        self.transport = self._make()
        await self.connect()

    async def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _ in range(100):
            result = await self.transport.request("tools/list", {"cursor": cursor} if cursor else {}, self.timeout)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict) -> dict:
        try:
            if not self.transport.alive:
                log.warning("mcp %s: сервер не работает, перезапуск", self.name)
                async with self.reconnect_lock:
                    if not self.transport.alive:
                        await self.reconnect()
            result = await self.transport.request("tools/call", {"name": name, "arguments": arguments}, self.timeout)
            self.last_error = ''
            return result
        except MCPError as exc:
            self.last_error = str(exc)
            raise

    async def close(self) -> None:
        await self.transport.close()


def tool_name(server: str, tool: str) -> str:
    """Имя для модели: только [A-Za-z0-9_-], не длиннее 64 символов (ограничение OpenAI API)."""
    clean = lambda s: re.sub(r"[^A-Za-z0-9_-]", "_", s)  # noqa: E731
    return f"mcp__{clean(server)}__{clean(tool)}"[:64]


def render_result(result: dict) -> ToolResult:
    parts = []
    for item in result.get("content") or []:
        kind = item.get("type")
        if kind == "text":
            parts.append(item.get("text", ""))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} {item.get('mimeType', '')}, {len(item.get('data', '')) * 3 // 4} байт]")
        elif kind == "resource":
            res = item.get("resource") or {}
            parts.append(res.get("text") or f"[ресурс {res.get('uri', '')}]")
        elif kind == "resource_link":
            parts.append(f"[ссылка на ресурс {item.get('uri', '')}]")
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return ToolResult("\n".join(parts) or "(пустой результат)", is_error=bool(result.get("isError")))


class MCPTool(Tool):
    default_permission = "ask"

    def __init__(self, client: MCPClient, spec: dict):
        self.client, self.remote_name = client, spec["name"]
        self.name = tool_name(client.name, spec["name"])
        self.description = f"[MCP {client.name}] " + (spec.get("description") or spec["name"])
        self.parameters = spec.get("inputSchema") or {"type": "object", "properties": {}}

    def check_args(self, args: dict) -> None:
        # Схемы MCP-серверов бывают сложнее нашего валидатора — проверяет сам сервер.
        if not isinstance(args, dict):
            raise ToolError("аргументы должны быть объектом")

    async def run(self, args: dict, ctx: ToolContext):
        try:
            return render_result(await self.client.call_tool(self.remote_name, args))
        except MCPError as exc:
            raise ToolError(f"MCP {self.client.name}: {exc}") from None


class MCPManager:
    """Все MCP-серверы из настроек. Сервер, который не поднялся, не мешает остальным."""

    def __init__(self, config: dict, proxy: str = ""):
        self.config, self.proxy = config, proxy
        self.clients: dict[str, MCPClient] = {}
        self.tools: list[MCPTool] = []
        self.errors: dict[str, str] = {}
        self.reconnect_lock = asyncio.Lock()

    async def start(self) -> "MCPManager":
        servers = {n: c for n, c in (self.config.get("servers") or {}).items() if c.get("enabled", True)}
        results = await asyncio.gather(*(self._start_one(n, c) for n, c in servers.items()))
        for name, tools in zip(servers, results):
            self.tools.extend(tools)
            if name not in self.errors:
                log.info("mcp %s: инструментов %d", name, len(tools))
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            await self.close()
            raise MCPError('коллизия имён MCP после нормализации; переименуйте сервер/инструмент')
        return self

    async def _start_one(self, name: str, cfg: dict) -> list[MCPTool]:
        client = None
        try:
            client = MCPClient(name, cfg, self.config.get("timeout", 60), self.proxy)
            await client.connect()
            specs = await client.list_tools()
        except (MCPError, ValueError, TypeError, AttributeError, KeyError) as exc:
            self.errors[name] = str(exc)
            log.warning("mcp %s: %s", name, exc)
            if client is not None:
                await client.close()
            return []
        self.clients[name] = client
        return [MCPTool(client, s) for s in specs if s.get("name")]

    def status(self, *, include_disabled=False) -> list[dict]:
        out = []
        for name, client in self.clients.items():
            alive = client.transport.alive
            error = client.last_error or ('' if alive else 'Соединение закрыто')
            state = 'error' if error else 'connected'
            out.append({'name': name, 'ok': not bool(error), 'state': state,
                        'tools': [t.name for t in self.tools if t.client is client],
                        'server': client.server_info, **({'error': error} if error else {})})
        out += [{'name': name, 'ok': False, 'state': 'error', 'error': error} for name, error in self.errors.items()]
        if include_disabled:
            out += [{'name': name, 'ok': False, 'state': 'disabled', 'tools': []}
                    for name, config in self.config.get('servers', {}).items() if not config.get('enabled', True)]
        return out

    async def close(self) -> None:
        await asyncio.gather(*(c.close() for c in self.clients.values()), return_exceptions=True)
        self.clients.clear()
        self.tools.clear()
