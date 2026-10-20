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
    assert events[-1]["type"] == "done" and events[-1]["reason"] == "stop"
    assert events[-1]["request_id"] and events[-1]["span_id"]
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


async def test_provider_switch_resume_and_missing_key(gw, monkeypatch):
    from agent.providers import ProviderManager
    cfg, ws, db = gw
    providers_file = ws / "providers.json"
    manager = ProviderManager(providers_file)
    url = json.loads(cfg.read_text())["gateway"]["url"]
    manager.add("first", url, "GW_TOKEN", "alpha", tags={"agent-free": ["free"]})
    manager.add("second", url, "GW_TOKEN", "beta")
    manager.add("broken", url, "MISSING_TEST_PROVIDER_KEY", "alpha")
    manager.use("first")
    monkeypatch.delenv("MISSING_TEST_PROVIDER_KEY", raising=False)
    SCRIPT.extend([{"content": "один"}, {"content": "два"}])
    out, err, read = run_cli(["/models", "первый", "/provider broken", "без ключа", "/provider second", "второй", "/exit"])
    args = ["--cwd", str(ws), "--settings", str(cfg), "--providers-file", str(providers_file)]
    assert await amain(args, read=read, out=out, err=err) == 0
    assert "first / agent-free · free" in out.getvalue()
    assert [r["body"]["model"] for r in SEEN if r["path"] == "chat"] == ["alpha", "beta"]
    assert "MISSING_TEST_PROVIDER_KEY" in err.getvalue()
    sessions = SessionStore(db).list("local")
    first = next(s for s in sessions if s.profile == "provider:first")
    second = next(s for s in sessions if s.profile == "provider:second")
    # Сохранённый провайдер важнее изменившегося default.
    SCRIPT.append({"content": "продолжаю"})
    out, err, read = run_cli([])
    assert await amain(args + ["--resume", second.id, "-p", "дальше"], read=read, out=out, err=err) == 0
    assert SEEN[-1]["body"]["model"] == "beta"
    assert [m["content"] for m in SEEN[-1]["body"]["messages"] if m["role"] == "user"] == ["второй", "дальше"]
    out, err, read = run_cli([])
    assert await amain(args + ["--provider", "second", "--resume", first.id, "-p", "x"], read=read, out=out, err=err) == 2
    assert "другому провайдеру" in err.getvalue()


async def test_provider_without_models_can_be_selected_and_model_set_later(gw, monkeypatch):
    from agent.llm import LLMClient
    from agent.providers import ProviderManager
    cfg, ws, db = gw
    providers_file = ws / "providers.json"
    manager = ProviderManager(providers_file)
    url = json.loads(cfg.read_text())["gateway"]["url"]
    manager.add("first", url, "GW_TOKEN", "alpha")
    manager.add("empty", url, "GW_TOKEN")
    async def empty_models(self):
        return []
    monkeypatch.setattr(LLMClient, "models", empty_models)
    SCRIPT.append({"content": "после ошибки"})
    out, err, read = run_cli(["/provider empty", "проверка", "/model alpha", "после настройки", "/exit"])
    assert await amain(["--cwd", str(ws), "--settings", str(cfg), "--provider", "first",
                        "--providers-file", str(providers_file)], read=read, out=out, err=err) == 0
    assert "ни одной модели" in err.getvalue()
    assert "после ошибки" in out.getvalue()
    sessions = SessionStore(db).list("local")
    assert any(s.profile == "provider:empty" and s.model == "alpha" for s in sessions)
    assert SEEN[-1]["body"]["model"] == "alpha"


async def test_plain_offline_commands_provider_add_set_resume_and_recovery(gw,monkeypatch):
    cfg,ws,db=gw
    monkeypatch.delenv('GW_TOKEN')
    out,err,read=run_cli(['/help','/tools','/models','/provider add local http://127.0.0.1:1/v1',
        '/provider local','/provider set local http://127.0.0.1:2/v1 custom','/model later','/new','/sessions','/exit'])
    assert await amain(['--cwd',str(ws),'--settings',str(cfg)],read=read,out=out,err=err)==0
    assert 'нет токена' in err.getvalue()
    assert 'Инструмент' not in err.getvalue()
    assert 'Подключение local сохранено' in out.getvalue()
    assert not SEEN
    store=SessionStore(db)
    session=next(s for s in store.list('local') if s.profile=='provider:local' and s.model=='later')
    store.close()
    out,err,read=run_cli(['/help','/exit'])
    assert await amain(['--cwd',str(ws),'--settings',str(cfg),'--resume',session.id],read=read,out=out,err=err)==0
    assert not SEEN


async def test_tool_help_works_offline_without_running_tool(gw,monkeypatch):
    cfg,ws,_=gw
    monkeypatch.delenv('GW_TOKEN')
    out,err,read=run_cli(['/tools write_file','/tools unknown','/exit'])
    assert await amain(['--cwd',str(ws),'--settings',str(cfg)],read=read,out=out,err=err)==0
    assert 'write_file' in out.getvalue() and '"required"' in out.getvalue()
    assert 'Неизвестный инструмент' in err.getvalue()
    assert not SEEN and not list(ws.iterdir())


async def test_direct_subagent_command_and_logs(gw):
    cfg, ws, db = gw
    SCRIPT.append({'content':'Результат дочерней задачи'})
    out, err, read = run_cli(['/agent Изучи задачу','/logs','/exit'])
    code = await amain(['--plain','--cwd',str(ws),'--settings',str(cfg),'--allow-all'],read=read,out=out,err=err)
    assert code==0, err.getvalue()
    assert 'Результат дочерней задачи' in out.getvalue()
    assert 'model.end' in out.getvalue() and 'request_id' in out.getvalue()
    store=SessionStore(db)
    assert len(store.list('local'))==2
    store.close()


async def test_compact_command_after_single_turn(gw):
    cfg, ws, db = gw
    SCRIPT.extend([{'content': 'Подробный старый ответ ' * 500}, {'content': 'Продолжаю'}])
    out, err, read = run_cli(['Исходная задача', '/compact', 'Следующий запрос', '/exit'])
    code = await amain(['--cwd', str(ws), '--settings', str(cfg)], read=read, out=out, err=err)
    assert code == 0
    assert 'Контекст сокращён' in out.getvalue()
    store = SessionStore(db)
    [session] = store.list('local')
    assert session.context_seq > 0
    assert len(store.messages(session.id)) == 4
    assert any(m.get('content') == 'Следующий запрос' for m in SEEN[-1]['body']['messages'])
    store.close()


async def test_plan_mode_and_model_question_in_plain_cli(gw):
    cfg, ws, db = gw
    SCRIPT.extend([{'tool_calls': [tool_call('q', 'question', question='Формат экспорта?', options=['JSON', 'CSV'])]},
                   {'content': 'Использую CSV'}])
    out, err, read = run_cli(['/mode plan', 'Составь план экспорта', '2', '/mode build', '/exit'])
    code = await amain(['--cwd', str(ws), '--settings', str(cfg)], read=read, out=out, err=err)
    assert code == 0
    assert 'Вопрос агента: Формат экспорта?' in out.getvalue()
    assert 'Активный агент: plan' in out.getvalue() and 'Активный агент: build' in out.getvalue()
    calls = [row['body'] for row in SEEN if 'body' in row]
    assert 'Активный агент: plan' in calls[0]['messages'][0]['content']
    assert json.loads(next(m['content'] for m in SEEN[-1]['body']['messages'] if m['role'] == 'tool')) == {'answer': 'CSV'}
    store = SessionStore(db); assert store.list('local')[0].agent_mode == 'build'; store.close()


async def test_connect_shell_command_and_eof(tmp_path):
    path = tmp_path / 'providers.json'
    out, err, read = run_cli(['', 'none'])
    code = await amain(['connect', 'gateway', '--providers-file', str(path), '--cwd', str(tmp_path)], read=read, out=out, err=err)
    assert code == 0, err.getvalue()
    from agent.providers import ProviderManager
    assert ProviderManager(path).connection().token == ''
    assert ProviderManager(path).get('gateway')['auth'] == 'stored'
    out, err, read = run_cli([])
    code = await amain(['connect', '--providers-file', str(path), '--cwd', str(tmp_path)], read=read, out=out, err=err)
    assert code == 2
    assert 'отменена' in err.getvalue() and 'Traceback' not in err.getvalue()


async def test_repl_connect_eof_does_not_crash(gw):
    cfg, ws, _ = gw
    out, err, read = run_cli(['/connect'])
    code = await amain(['--cwd', str(ws), '--settings', str(cfg)], read=read, out=out, err=err)
    assert code == 0
    assert 'отменена' in err.getvalue()
