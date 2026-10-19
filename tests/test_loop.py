"""Цикл агента со сценарным LLM и игрушечными инструментами. См. docs/wiki/agent-loop.md."""

import asyncio
import json
import shlex
import sys

import httpx

from agent.hooks import Hooks
from agent.llm import LLMClient
from agent.loop import CANCELLED, Agent
from agent.settings import DEFAULTS, merge
from agent.store import SessionStore
from agent.tools.base import Registry, Tool, ToolError
from tests.fakes import scripted_transport, tool_call


class Echo(Tool):
    name, description, default_permission = "echo", "повторяет текст", "allow"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    async def run(self, args, ctx):
        return "эхо: " + args["text"]


class Danger(Tool):
    name, description = "danger", "требует подтверждения"
    parameters = {"type": "object", "properties": {"target": {"type": "string"}}}

    def subject(self, args):
        return args.get("target")

    async def run(self, args, ctx):
        return "сделано " + args.get("target", "")


class Fail(Tool):
    name, default_permission = "fail", "allow"

    async def run(self, args, ctx):
        if args.get("expected"):
            raise ToolError("ожидаемая ошибка")
        raise RuntimeError("внезапно")


class Slow(Tool):
    name, default_permission = "slow", "allow"

    async def run(self, args, ctx):
        await asyncio.sleep(10)


def make_agent(tmp_path, script, seen=None, approver=None, hooks=None, **settings):
    store = SessionStore(tmp_path / "s.db")
    session = store.create("local", model="alpha", workspace=str(tmp_path))
    llm = LLMClient(httpx.AsyncClient(base_url="http://up/v1", transport=scripted_transport(script, seen)))
    usage = []
    agent = Agent(llm=llm, registry=Registry([Echo(), Danger(), Fail(), Slow()]),
                  settings=merge(DEFAULTS, settings), store=store, session=session,
                  hooks=Hooks(hooks or {}, cwd=str(tmp_path), session_id=session.id),
                  approver=approver, on_usage=lambda *a: usage.append(a))
    return agent, store, usage


async def run(agent, text="задача"):
    return [ev async for ev in agent.run(text)]


def types(events):
    return [e["type"] for e in events]


async def test_plain_answer(tmp_path):
    seen = []
    agent, store, usage = make_agent(tmp_path, [{"content": "готово"}], seen)
    evs = await run(agent, "привет\nвторая строка")
    assert types(evs) == ["text", "usage", "assistant", "done"]
    assert evs[-1]["reason"] == "stop"
    assert [m["role"] for m in store.messages(agent.session.id)] == ["user", "assistant"]
    assert store.get(agent.session.id).title == "привет"
    sent = seen[0]["messages"]
    assert sent[0]["role"] == "system" and str(tmp_path) in sent[0]["content"]
    assert {t["function"]["name"] for t in seen[0]["tools"]} == {"echo", "danger", "fail", "slow"}
    assert usage == [("alpha", 200, (7, 3))]


async def test_tool_chain_and_history_sent_back(tmp_path):
    seen = []
    script = [{"content": "сейчас", "tool_calls": [tool_call("c1", "echo", text="a"), tool_call("c2", "echo", text="b")]},
              {"content": "итог"}]
    agent, store, _ = make_agent(tmp_path, script, seen)
    evs = await run(agent)
    results = [e for e in evs if e["type"] == "tool_result"]
    assert [r["content"] for r in results] == ["эхо: a", "эхо: b"]
    assert all(r["ok"] and r["decision"] == "allow" for r in results)
    second = seen[1]["messages"]
    assert [m["role"] for m in second[1:]] == ["user", "assistant", "tool", "tool"]
    assert second[3]["tool_call_id"] == "c1"
    assert [r["tool"] for r in store.tool_runs(agent.session.id)] == ["echo", "echo"]


async def test_tool_errors_reach_model(tmp_path):
    script = [{"tool_calls": [tool_call("c1", "fail", expected=True), tool_call("c2", "fail"),
                              tool_call("c3", "nope"), tool_call("c4", "echo"),
                              {"id": "c5", "name": "echo", "arguments": "{битый"}]},
              {"content": "понял"}]
    agent, store, _ = make_agent(tmp_path, script)
    evs = await run(agent)
    res = {e["id"]: e for e in evs if e["type"] == "tool_result"}
    assert not any(r["ok"] for r in res.values())
    assert "ожидаемая ошибка" in res["c1"]["content"]
    assert "RuntimeError: внезапно" in res["c2"]["content"]
    assert "нет инструмента" in res["c3"]["content"]
    assert "text: обязательное поле" in res["c4"]["content"]
    assert "не JSON" in res["c5"]["content"]
    assert evs[-1]["reason"] == "stop"


async def test_max_steps(tmp_path):
    script = [{"tool_calls": [tool_call(f"c{i}", "echo", text="x")]} for i in range(5)]
    agent, _, _ = make_agent(tmp_path, script, max_steps=2)
    evs = await run(agent)
    assert evs[-1] == {"type": "done", "reason": "max_steps"}
    assert "лимит шагов" in evs[-2]["message"]


async def test_llm_error(tmp_path):
    agent, _, usage = make_agent(tmp_path, [{"status": 503, "error": "перегружен"}])
    evs = await run(agent)
    assert evs[-2]["type"] == "error" and "перегружен" in evs[-2]["message"]
    assert evs[-1]["reason"] == "error"
    assert usage == [("alpha", 503, (0, 0))]


async def test_output_truncated(tmp_path):
    script = [{"tool_calls": [tool_call("c1", "echo", text="я" * 500)]}, {"content": "ok"}]
    agent, _, _ = make_agent(tmp_path, script, max_tool_output_chars=100)
    res = next(e for e in await run(agent) if e["type"] == "tool_result")
    assert "обрезано" in res["content"] and len(res["content"]) < 200


async def test_permission_ask_allow_deny_always(tmp_path):
    answers = iter(["deny", "allow", "always"])
    asked = []

    async def approver(req):
        asked.append(req)
        return next(answers)

    script = [{"tool_calls": [tool_call("c1", "danger", target="x"), tool_call("c2", "danger", target="x"),
                              tool_call("c3", "danger", target="y"), tool_call("c4", "danger", target="y")]},
              {"content": "ok"}]
    agent, store, _ = make_agent(tmp_path, script, approver=approver)
    evs = await run(agent)
    res = {e["id"]: e for e in evs if e["type"] == "tool_result"}
    assert res["c1"]["decision"] == "ask:deny" and "отклонено пользователем" in res["c1"]["content"]
    assert res["c2"]["decision"] == "ask:allow" and res["c2"]["content"] == "сделано x"
    assert res["c3"]["decision"] == "ask:allow"
    assert res["c4"]["decision"] == "allow"          # «всегда» сработало без вопроса
    assert len(asked) == 3 and asked[0]["rule"] == "danger(x)"
    assert store.get(agent.session.id).allow_rules == ["danger(y)"]


async def test_ask_without_approver_denies_and_modes(tmp_path):
    script = [{"tool_calls": [tool_call("c1", "danger")]}, {"content": "ok"}]
    agent, _, _ = make_agent(tmp_path, list(script))
    res = next(e for e in await run(agent) if e["type"] == "tool_result")
    assert res["decision"] == "ask:deny"
    agent, _, _ = make_agent(tmp_path / "2", list(script), permissions={"mode": "allow_all"})
    assert next(e for e in await run(agent) if e["type"] == "tool_result")["ok"]
    agent, _, _ = make_agent(tmp_path / "3", list(script), permissions={"mode": "read_only"})
    assert next(e for e in await run(agent) if e["type"] == "tool_result")["decision"] == "deny"


async def test_cancel_keeps_history_valid(tmp_path):
    script = [{"tool_calls": [tool_call("c1", "echo", text="a"), tool_call("c2", "slow"), tool_call("c3", "echo", text="b")]}]
    agent, store, _ = make_agent(tmp_path, script)

    async def consume():
        async for _ in agent.run("x"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    msgs = store.messages(agent.session.id)
    tool_msgs = {m["tool_call_id"]: m["content"] for m in msgs if m["role"] == "tool"}
    assert tool_msgs == {"c1": "эхо: a", "c2": CANCELLED, "c3": CANCELLED}


def hook_cmd(script):
    return f"{sys.executable} -c {shlex.quote(script)}"


async def test_hooks_in_loop(tmp_path):
    # PreToolUse меняет аргументы, PostToolUse добавляет контекст, SessionStart — в системный промпт.
    pre = hook_cmd("import json,sys; d=json.load(sys.stdin); print(json.dumps({'updated_input': {'text': d['tool_input']['text'].upper()}}))")
    post = hook_cmd("print('{\"additional_context\": \"проверено\"}')")
    start = hook_cmd("print('{\"additional_context\": \"проект: тест\"}')")
    seen = []
    script = [{"tool_calls": [tool_call("c1", "echo", text="abc")]}, {"content": "ok"}]
    agent, _, _ = make_agent(tmp_path, script, seen, hooks={
        "PreToolUse": [{"matcher": "echo", "command": pre}],
        "PostToolUse": [{"matcher": "ech.*", "command": post}],
        "SessionStart": [{"command": start}],
    })
    evs = await run(agent)
    res = next(e for e in evs if e["type"] == "tool_result")
    assert res["content"].startswith("эхо: ABC") and "проверено" in res["content"]
    assert "проект: тест" in seen[0]["messages"][0]["content"]


async def test_prompt_hook_blocks_and_stop_hook_continues(tmp_path):
    block = hook_cmd("import sys; print('нельзя', file=sys.stderr); sys.exit(2)")
    agent, store, _ = make_agent(tmp_path, [], hooks={"UserPromptSubmit": [{"command": block}]})
    evs = await run(agent)
    assert evs[-1]["reason"] == "blocked" and "нельзя" in evs[-2]["message"]
    assert store.messages(agent.session.id) == []

    flag = tmp_path / "once"
    stop = hook_cmd(f"import os,sys,json; p={str(flag)!r}\n"
                    "if not os.path.exists(p):\n open(p,'w').close(); print(json.dumps({'decision':'deny','reason':'проверь ещё раз'}))")
    agent, store, _ = make_agent(tmp_path / "b", [{"content": "первый"}, {"content": "второй"}],
                                 hooks={"Stop": [{"command": stop}]})
    evs = await run(agent)
    assert [e["content"] for e in evs if e["type"] == "assistant"] == ["первый", "второй"]
    assert [m["content"] for m in store.messages(agent.session.id) if m["role"] == "user"][-1] == "проверь ещё раз"
