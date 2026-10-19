"""Хуки-команды: протокол stdin/stdout/коды выхода. См. docs/wiki/hooks.md."""

import json
import shlex
import sys

from agent.hooks import Hooks


def py(script):
    return f"{sys.executable} -c {shlex.quote(script)}"


async def run(tmp_path, config, event="PreToolUse", tool="shell", payload=None):
    hooks = Hooks(config, cwd=str(tmp_path), session_id="s1")
    return await hooks.run(event, payload or {"tool_name": tool, "tool_input": {"command": "ls"}}, tool)


async def test_receives_event_json_and_env(tmp_path):
    out = tmp_path / "got.json"
    cmd = py(f"import json,os,sys; d=json.load(sys.stdin); d['env']=os.environ['GWAGENT_EVENT']; "
             f"open({str(out)!r},'w').write(json.dumps(d))")
    res = await run(tmp_path, {"PreToolUse": [{"command": cmd}]})
    got = json.loads(out.read_text())
    assert got["event"] == "PreToolUse" and got["env"] == "PreToolUse"
    assert got["session_id"] == "s1" and got["cwd"] == str(tmp_path)
    assert got["tool_input"] == {"command": "ls"}
    assert res.decision is None and not res.warnings


async def test_exit_2_blocks_with_stderr(tmp_path):
    res = await run(tmp_path, {"PreToolUse": [{"command": "echo 'опасно' >&2; exit 2"}]})
    assert res.blocked and res.reason == "опасно"


async def test_other_codes_warn_but_continue(tmp_path):
    res = await run(tmp_path, {"PreToolUse": [{"command": "echo упал >&2; exit 1"}]})
    assert res.decision is None and "код 1" in res.warnings[0]
    res = await run(tmp_path, {"PreToolUse": [{"command": "/nonexistent/hook"}]})
    assert res.decision is None and res.warnings


async def test_timeout(tmp_path):
    res = await run(tmp_path, {"PreToolUse": [{"command": "sleep 5", "timeout": 1}]})
    assert "таймаут" in res.warnings[0]


async def test_json_answer_and_matcher(tmp_path):
    cfg = {"PreToolUse": [
        {"matcher": "web_.*", "command": "exit 2"},             # не про shell
        {"matcher": "shell", "command": "echo '{\"decision\": \"ask\", \"reason\": \"проверь\"}'"},
        {"command": "echo '{\"decision\": \"allow\", \"additional_context\": \"а\"}'"},
        {"matcher": "*", "command": "echo не json"},
    ]}
    res = await run(tmp_path, cfg)
    assert (res.decision, res.reason) == ("ask", "проверь")  # самое строгое решение побеждает
    assert res.context == "а"


async def test_updated_input_chains(tmp_path):
    first = py("import json,sys; d=json.load(sys.stdin); d['tool_input']['command']+=' -l'; "
               "print(json.dumps({'updated_input': d['tool_input']}))")
    second = py("import json,sys; d=json.load(sys.stdin); d['tool_input']['command']+=' -a'; "
                "print(json.dumps({'updated_input': d['tool_input']}))")
    res = await run(tmp_path, {"PreToolUse": [{"command": first}, {"command": second}]})
    assert res.updated_input == {"command": "ls -l -a"}


async def test_deny_stops_chain(tmp_path):
    marker = tmp_path / "ran"
    res = await run(tmp_path, {"PreToolUse": [{"command": "exit 2"}, {"command": f"touch {marker}"}]})
    assert res.blocked and not marker.exists()


async def test_no_hooks_for_event(tmp_path):
    res = await run(tmp_path, {"PostToolUse": [{"command": "exit 2"}]})
    assert res.decision is None
