"""CLI gwagent против настоящего шлюза (uvicorn в потоке) и mTLS-апстрима со сценарием. См. docs/wiki/cli.md."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent import settings as st
from agent.cli import amain
from agent.store import SessionStore
from gateway.app import create_app
from gateway.tokens import TokenStore
from tests.fakes import SCRIPT, SEEN, ServerThread, tool_call

ROOT = Path(__file__).resolve().parent.parent
MCP = str(ROOT / "tests" / "fixtures" / "mcp_stdio_server.py")


@pytest.fixture
def gw(gateway_settings, tmp_path, monkeypatch):
    """Шлюз на свободном порту + файл настроек CLI; возвращает (путь к настройкам, рабочая папка, база сессий)."""
    settings = gateway_settings()
    tokens = TokenStore(settings.db_path)
    raw, _ = tokens.create("cli")
    monkeypatch.setenv("GW_TOKEN", raw)
    monkeypatch.setattr(st, "USER_SETTINGS", tmp_path / "no-user-settings.json")
    SEEN.clear()
    SCRIPT.clear()
    with ServerThread(create_app(settings, tokens)) as srv:
        cfg = tmp_path / "cli.json"
        db = tmp_path / "sessions.db"
        cfg.write_text(json.dumps({"gateway": {"url": f"http://127.0.0.1:{srv.port}/v1"},
                                   "sessions": {"db": str(db)}}))
        ws = tmp_path / "ws"
        ws.mkdir()
        yield cfg, ws, db
    SCRIPT.clear()


def run_cli(answers=()):
    out, err = io.StringIO(), io.StringIO()
    feed = iter(answers)

    def read(prompt=""):
        out.write(prompt)
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    return out, err, read


async def test_prompt_mode_with_tools(gw):
    cfg, ws, db = gw
    SCRIPT.extend([{"content": "Пишу", "tool_calls": [tool_call("c1", "write_file", path="hello.txt", content="hi")]},
                   {"content": "Готово"}])
    out, err, read = run_cli([])
    code = await amain(["-p", "создай hello.txt", "--cwd", str(ws), "--settings", str(cfg), "--allow-all"],
                       read=read, out=out, err=err)
    assert code == 0, err.getvalue()
    assert (ws / "hello.txt").read_text() == "hi"
    text = out.getvalue()
    assert "Пишу" in text and "⚙ write_file" in text and "✓ Создан hello.txt" in text and "Готово" in text
    assert SEEN[-1]["body"]["model"] == "alpha"          # первая модель из /v1/models
    [session] = SessionStore(db).list("local")
    assert session.workspace == str(ws) and session.title == "создай hello.txt"


async def test_prompt_mode_denies_without_tty_and_json_output(gw):
    cfg, ws, _ = gw
    SCRIPT.extend([{"tool_calls": [tool_call("c1", "shell", command="touch x")]}, {"content": "не смог"}])
    out, err, read = run_cli([])
    code = await amain(["-p", "x", "--cwd", str(ws), "--settings", str(cfg), "--json", "--model", "beta"],
                       read=read, out=out, err=err)
    assert code == 0
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["decision"] == "ask:deny" and not (ws / "x").exists()
    assert events[-1] == {"type": "done", "reason": "stop"}
    assert SEEN[-1]["body"]["model"] == "beta"


async def test_repl_approvals_commands_and_resume(gw):
    cfg, ws, db = gw
    SCRIPT.extend([{"tool_calls": [tool_call("c1", "shell", command="echo один")]}, {"content": "раз"},
                   {"tool_calls": [tool_call("c2", "shell", command="echo один")]}, {"content": "два"}])
    out, err, read = run_cli(["первый", "a", "второй", "/sessions", "/tools", "/model gamma", "/nope", "/exit"])
    code = await amain(["--cwd", str(ws), "--settings", str(cfg)], read=read, out=out, err=err)
    assert code == 0
    text = out.getvalue()
    assert text.count("Разрешить?") == 1           # «всегда» — второй раз без вопроса
    assert "shell(echo один)" in text and "раз" in text and "два" in text
    assert "read_file" in text and "Модель: gamma" in text
    assert "Неизвестная команда /nope" in err.getvalue()
    [session] = SessionStore(db).list("local")
    assert session.allow_rules == ["shell(echo один)"] and session.model == "gamma"

    SCRIPT.append({"content": "продолжаю"})
    out, err, read = run_cli([])
    code = await amain(["-c", "-p", "дальше", "--cwd", str(ws), "--settings", str(cfg)], read=read, out=out, err=err)
    assert code == 0
    assert len(SessionStore(db).list("local")) == 1
    history = SEEN[-1]["body"]["messages"]
    assert [m["content"] for m in history if m["role"] == "user"] == ["первый", "второй", "дальше"]


async def test_mcp_command_and_tool(gw):
    cfg, ws, _ = gw
    data = json.loads(cfg.read_text())
    data["mcp"] = {"servers": {"test": {"type": "stdio", "command": sys.executable, "args": [MCP]},
                               "broken": {"type": "stdio", "command": "/nonexistent"}}}
    data["permissions"] = {"allow": ["mcp__test__*"]}
    cfg.write_text(json.dumps(data))
    SCRIPT.extend([{"tool_calls": [tool_call("c1", "mcp__test__echo", text="из MCP")]}, {"content": "ok"}])
    out, err, read = run_cli(["/mcp", "проверь", "/exit"])
    assert await amain(["--cwd", str(ws), "--settings", str(cfg)], read=read, out=out, err=err) == 0
    text = out.getvalue()
    assert "✓ test: 6 инстр." in text and "✗ broken" in text and "✓ из MCP" in text
    assert "MCP broken" in err.getvalue()


async def test_errors(gw, monkeypatch, tmp_path):
    cfg, ws, _ = gw
    out, err, read = run_cli([])
    assert await amain(["--resume", "nope", "--cwd", str(ws), "--settings", str(cfg)], read=read, out=out, err=err) == 2
    assert "не найдена" in err.getvalue()
    monkeypatch.delenv("GW_TOKEN")
    out, err, read = run_cli([])
    assert await amain(["-p", "x", "--cwd", str(ws), "--settings", str(cfg)], read=read, out=out, err=err) == 2
    assert "нет токена" in err.getvalue()
    out, err, read = run_cli([])
    assert await amain(["--cwd", str(tmp_path / "nope")], read=read, out=out, err=err) == 2
    SCRIPT.append({"status": 500, "error": "упало"})
    monkeypatch.setenv("GW_TOKEN", "gw-bad")
    out, err, read = run_cli([])
    assert await amain(["-p", "x", "--model", "alpha", "--cwd", str(ws), "--settings", str(cfg)],
                       read=read, out=out, err=err) == 1
    assert "401" in err.getvalue()


def test_launcher_script(tmp_path):
    env = {**os.environ, "GW_TOKEN": ""}
    r = subprocess.run([str(ROOT / "gwagent"), "--help"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "Терминальный агент" in r.stdout
