#!/usr/bin/env python3
"""Игрушечный MCP-сервер (stdio) для тестов и ручной проверки gwagent.

Инструменты: echo, add, fail (isError), crash (процесс умирает), slow (спит), image.
tools/list отдаётся двумя страницами (проверка nextCursor). Перед ответом на tools/call
сервер шлёт уведомление и ping — клиент должен это пережить.
"""

import json
import sys
import time

TOOLS = [
    {"name": "echo", "description": "Повторить текст",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Сложить два числа",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
    {"name": "fail", "description": "Всегда ошибка", "inputSchema": {"type": "object"}},
    {"name": "crash", "description": "Упасть", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "Долго думать", "inputSchema": {"type": "object"}},
    {"name": "image", "description": "Картинка", "inputSchema": {"type": "object"}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(rid, value):
    send({"jsonrpc": "2.0", "id": rid, "result": value})


def text(t, error=False):
    return {"content": [{"type": "text", "text": t}], "isError": error}


print("mcp-test: старт", file=sys.stderr, flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None:          # ответ на наш ping
        continue
    if method == "initialize":
        result(rid, {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                     "serverInfo": {"name": "mcp-test", "version": "1.0"}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        if params.get("cursor") == "page2":
            result(rid, {"tools": TOOLS[3:]})
        else:
            result(rid, {"tools": TOOLS[:3], "nextCursor": "page2"})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "x"}})
        send({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"})
        name, args = params["name"], params.get("arguments") or {}
        if name == "echo":
            result(rid, text(args.get("text", "")))
        elif name == "add":
            result(rid, {"content": [], "structuredContent": {"sum": args["a"] + args["b"]}})
        elif name == "fail":
            result(rid, text("что-то сломалось", error=True))
        elif name == "crash":
            print("mcp-test: падаю", file=sys.stderr, flush=True)
            sys.exit(1)
        elif name == "slow":
            time.sleep(5)
            result(rid, text("проснулся"))
        elif name == "image":
            result(rid, {"content": [{"type": "image", "mimeType": "image/png", "data": "AAAA"},
                                     {"type": "text", "text": "подпись"}]})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"нет инструмента {name}"}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
