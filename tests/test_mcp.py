"""MCP-клиент: stdio и Streamable HTTP против игрушечных серверов. См. docs/wiki/mcp.md."""

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from agent.mcp import MCPClient, MCPError, MCPManager, render_result, subst, tool_name
from agent.tools.base import ToolContext, ToolError
from tests.fakes import ServerThread

STDIO = {"type": "stdio", "command": sys.executable,
         "args": [str(Path(__file__).parent / "fixtures" / "mcp_stdio_server.py")]}


async def started(config):
    return await MCPManager(config).start()


def by_name(manager):
    return {t.remote_name: t for t in manager.tools}


async def test_stdio_list_and_call(tmp_path):
    m = await started({"servers": {"test": STDIO}})
    try:
        tools = by_name(m)
        assert set(tools) == {"echo", "add", "fail", "crash", "slow", "image"}   # обе страницы
        assert tools["echo"].name == "mcp__test__echo"
        assert tools["echo"].description == "[MCP test] Повторить текст"
        ctx = ToolContext(workspace=tmp_path, settings={})
        assert (await tools["echo"].run({"text": "привет"}, ctx)).content == "привет"
        assert (await tools["add"].run({"a": 2, "b": 3}, ctx)).content == '{"sum": 5}'
        failed = await tools["fail"].run({}, ctx)
        assert failed.is_error and failed.content == "что-то сломалось"
        assert (await tools["image"].run({}, ctx)).content == "[image image/png, 3 байт]\nподпись"
        assert m.status()[0]["server"] == {"name": "mcp-test", "version": "1.0"}
    finally:
        await m.close()


async def test_stdio_crash_reports_stderr_then_restarts(tmp_path):
    m = await started({"servers": {"test": STDIO}})
    try:
        tools, ctx = by_name(m), ToolContext(workspace=tmp_path, settings={})
        with pytest.raises(ToolError, match="завершился.*падаю"):
            await tools["crash"].run({}, ctx)
        assert (await tools["echo"].run({"text": "снова"}, ctx)).content == "снова"   # перезапущен
    finally:
        await m.close()


async def test_timeout_and_rpc_error():
    client = MCPClient("test", {**STDIO, "timeout": 1})
    await client.connect()
    try:
        with pytest.raises(MCPError, match="нет инструмента zzz.*-32602"):
            await client.call_tool("zzz", {})
        with pytest.raises(MCPError, match="не ответил за 1"):
            await client.call_tool("slow", {})
    finally:
        await client.close()


async def test_bad_servers_do_not_break_others():
    m = await started({"servers": {
        "ok": STDIO,
        "missing": {"type": "stdio", "command": "/nonexistent/mcp"},
        "nocmd": {"type": "stdio"},
        "off": {**STDIO, "enabled": False},
    }})
    try:
        status = {s["name"]: s for s in m.status()}
        assert status["ok"]["ok"] and "off" not in status
        assert "не удалось запустить" in status["missing"]["error"]
        assert "нужен command" in status["nocmd"]["error"]
    finally:
        await m.close()


# ---------- Streamable HTTP ----------

LOG: list = []
http_app = FastAPI()


@http_app.post("/mcp")
async def _mcp(request: Request):
    msg = await request.json()
    LOG.append({"method": msg.get("method"), "session": request.headers.get("mcp-session-id"),
                "auth": request.headers.get("authorization"), "proto": request.headers.get("mcp-protocol-version")})
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "http-test"}}},
            headers={"mcp-session-id": "sess-42"})
    if request.headers.get("mcp-session-id") != "sess-42":
        return Response(status_code=400)
    if rid is None:
        return Response(status_code=202)
    if method == "tools/list":
        async def sse():   # ответ потоком: сначала чужое уведомление, потом наш ответ
            yield "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"}) + "\n\n"
            yield "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                "tools": [{"name": "time.now", "inputSchema": {"type": "object"}}]}}) + "\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream")
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "12:00"}]}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "нет"}}


@http_app.delete("/mcp")
async def _close(request: Request):
    LOG.append({"method": "DELETE", "session": request.headers.get("mcp-session-id")})
    return Response(status_code=200)


async def test_http_transport(tmp_path, monkeypatch):
    LOG.clear()
    monkeypatch.setenv("MCP_TOKEN", "s3cret")
    with ServerThread(http_app) as srv:
        m = await started({"servers": {"remote": {
            "type": "http", "url": f"http://127.0.0.1:{srv.port}/mcp",
            "headers": {"authorization": "Bearer ${MCP_TOKEN}"}}}})
        try:
            [tool] = m.tools
            assert tool.name == "mcp__remote__time_now"
            out = await tool.run({}, ToolContext(workspace=tmp_path, settings={}))
            assert out.content == "12:00"
        finally:
            await m.close()
    methods = [e["method"] for e in LOG]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call", "DELETE"]
    assert all(e["session"] == "sess-42" for e in LOG[1:])
    assert LOG[0]["auth"] == "Bearer s3cret" and LOG[2]["proto"] == "2025-06-18"


async def test_http_unreachable():
    m = await started({"servers": {"down": {"type": "http", "url": "http://127.0.0.1:9/mcp"}}})
    assert not m.tools and "down" in m.errors


def test_helpers(monkeypatch):
    monkeypatch.setenv("X1", "v")
    assert subst({"a": ["${X1}", "$X1/b"], "n": 1}) == {"a": ["v", "v/b"], "n": 1}
    assert tool_name("my server", "do.it") == "mcp__my_server__do_it"
    assert len(tool_name("s" * 50, "t" * 50)) == 64
    assert render_result({"content": [{"type": "resource", "resource": {"uri": "file:///a", "text": "txt"}},
                                      {"type": "resource_link", "uri": "file:///b"}]}).content == \
        "txt\n[ссылка на ресурс file:///b]"
    assert render_result({}).content == "(пустой результат)"
