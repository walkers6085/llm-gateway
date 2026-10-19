"""Агент на сервере (/agent/*) через настоящий mTLS-апстрим со сценарием. См. docs/wiki/server-api.md."""

import json
import threading
import time
from pathlib import Path

import pytest

from tests.fakes import SCRIPT, SEEN, auth, tool_call

PROFILES = {"profiles": {
    "default": {"workspace_root": "WS"},
    "auto": {"workspace_root": "WS", "permissions": {"mode": "allow_all"}},
    "ro": {"workspace_root": "WS", "permissions": {"mode": "read_only"}, "tools": {"enabled": ["read_file"]}},
}}


@pytest.fixture
def agent_gw(make, tmp_path):
    def _make(**overrides):
        cfg = json.loads(json.dumps(PROFILES).replace("WS", str(tmp_path / "ws")))
        path = tmp_path / "agent.json"
        path.write_text(json.dumps(cfg))
        return make(agent_config=str(path), **overrides)
    return _make


def events(text):
    out = []
    for block in text.split("\n\n"):
        data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            out.append(json.loads("".join(data)))
    return out


def send(client, raw, sid, content):
    with client.stream("POST", f"/agent/sessions/{sid}/messages", json={"content": content}, headers=auth(raw)) as r:
        assert r.status_code == 200, r.read()
        return events("".join(r.iter_text()))


def test_access_requires_agent_profile(agent_gw):
    client, store = agent_gw()
    plain, _ = store.create("plain")
    ghost, _ = store.create("ghost", agent_profile="nope")
    with client:
        r = client.post("/agent/sessions", json={"model": "alpha"}, headers=auth(plain))
        assert r.status_code == 403 and r.json()["error"]["code"] == "agent_not_allowed"
        r = client.get("/agent/sessions", headers=auth(ghost))
        assert r.json()["error"]["code"] == "agent_profile_missing"
        assert client.get("/agent/sessions").status_code == 401


def test_bad_config_gives_503(make, tmp_path):
    bad = tmp_path / "agent.json"
    bad.write_text('{"profiles": {"default": {"max_steps": "много"}}}')
    client, store = make(agent_config=str(bad))
    raw, _ = store.create("a", agent_profile="default")
    with client:
        r = client.get("/agent/sessions", headers=auth(raw))
    assert r.status_code == 503 and "max_steps" in r.json()["error"]["message"]


def test_full_cycle_with_tools_and_usage(agent_gw, tmp_path):
    client, store = agent_gw()
    raw, token = store.create("alice", agent_profile="auto")
    SCRIPT.extend([
        {"content": "Пишу файл", "tool_calls": [tool_call("c1", "write_file", path="hello.txt", content="hi")]},
        {"content": "Готово"},
    ])
    with client:
        s = client.post("/agent/sessions", json={"model": "alpha"}, headers=auth(raw)).json()
        evs = send(client, raw, s["id"], "создай hello.txt")
        detail = client.get(f"/agent/sessions/{s['id']}", headers=auth(raw)).json()
        listing = client.get("/agent/sessions", headers=auth(raw)).json()["data"]
        tools = client.get("/agent/tools", headers=auth(raw)).json()
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "session" and kinds[-1] == "done" and evs[-1]["reason"] == "stop"
    result = next(e for e in evs if e["type"] == "tool_result")
    assert result["ok"] and result["decision"] == "allow"
    ws = Path(s["workspace"])
    assert ws == tmp_path / "ws" / str(token.id) / s["id"]
    assert (ws / "hello.txt").read_text() == "hi"
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert detail["title"] == "создай hello.txt" and not detail["running"]
    assert listing[0]["id"] == s["id"]
    assert SEEN[-1]["auth"] is None and SEEN[-1]["body"]["tools"]   # токен пользователя наверх не уходит
    row = store.usage(token.id)[0]
    assert (row["model"], row["requests"], row["prompt_tokens"]) == ("alpha", 2, 14)
    assert {"read_file", "shell", "web_search"} <= {t["name"] for t in tools["data"]}


def test_approval_flow(agent_gw):
    client, store = agent_gw()
    raw, _ = store.create("bob", agent_profile="default")
    SCRIPT.extend([{"tool_calls": [tool_call("c1", "shell", command="echo секрет")]}, {"content": "ок"}])
    with client:
        sid = client.post("/agent/sessions", json={"model": "alpha"}, headers=auth(raw)).json()["id"]
        collected = []

        def reader():
            collected.extend(send(client, raw, sid, "выполни"))

        t = threading.Thread(target=reader)
        t.start()
        for _ in range(100):
            pending = client.get(f"/agent/sessions/{sid}", headers=auth(raw)).json()["pending"]
            if pending:
                break
            time.sleep(0.05)
        assert pending == ["c1"]
        busy = client.post(f"/agent/sessions/{sid}/messages", json={"content": "ещё"}, headers=auth(raw))
        assert busy.status_code == 409
        bad = client.post(f"/agent/sessions/{sid}/approve", json={"id": "zzz", "decision": "allow"}, headers=auth(raw))
        assert bad.status_code == 404
        ok = client.post(f"/agent/sessions/{sid}/approve", json={"id": "c1", "decision": "always"}, headers=auth(raw))
        assert ok.status_code == 200
        t.join(10)
        rules = client.get(f"/agent/sessions/{sid}", headers=auth(raw)).json()["allow_rules"]
    req = next(e for e in collected if e["type"] == "permission_request")
    assert req["rule"] == "shell(echo секрет)"
    res = next(e for e in collected if e["type"] == "tool_result")
    assert res["decision"] == "ask:allow" and "секрет" in res["content"]
    assert rules == ["shell(echo секрет)"]


def test_cancel(agent_gw):
    client, store = agent_gw()
    raw, _ = store.create("carl", agent_profile="default")
    SCRIPT.extend([{"tool_calls": [tool_call("c1", "shell", command="sleep 1")]}])
    with client:
        sid = client.post("/agent/sessions", json={"model": "alpha"}, headers=auth(raw)).json()["id"]
        collected = []
        t = threading.Thread(target=lambda: collected.extend(send(client, raw, sid, "жди")))
        t.start()
        for _ in range(100):
            if client.get(f"/agent/sessions/{sid}", headers=auth(raw)).json()["pending"]:
                break
            time.sleep(0.05)
        assert client.post(f"/agent/sessions/{sid}/cancel", headers=auth(raw)).json() == {"cancelled": True}
        t.join(10)
        msgs = client.get(f"/agent/sessions/{sid}", headers=auth(raw)).json()["messages"]
        assert client.post(f"/agent/sessions/{sid}/cancel", headers=auth(raw)).json() == {"cancelled": False}
    assert collected[-1] == {"type": "done", "reason": "cancelled"}
    assert msgs[-1] == {"role": "tool", "tool_call_id": "c1", "content": "Отменено пользователем"}


def test_isolation_model_allowlist_delete(agent_gw):
    client, store = agent_gw()
    a, _ = store.create("a", agent_profile="default")
    b, _ = store.create("b", agent_profile="default", models=["beta"])
    with client:
        s = client.post("/agent/sessions", json={"model": "alpha"}, headers=auth(a)).json()
        assert client.get(f"/agent/sessions/{s['id']}", headers=auth(b)).status_code == 404
        assert client.delete(f"/agent/sessions/{s['id']}", headers=auth(b)).status_code == 404
        r = client.post("/agent/sessions", json={"model": "alpha"}, headers=auth(b))
        assert r.json()["error"]["code"] == "model_not_allowed"
        assert client.post("/agent/sessions", json={}, headers=auth(a)).json()["error"]["code"] == "model_required"
        assert client.post(f"/agent/sessions/{s['id']}/messages", json={"content": " "},
                           headers=auth(a)).status_code == 400
        assert Path(s["workspace"]).is_dir()
        assert client.delete(f"/agent/sessions/{s['id']}", headers=auth(a)).json() == {"deleted": s["id"]}
        assert client.get(f"/agent/sessions/{s['id']}", headers=auth(a)).status_code == 404
    assert not Path(s["workspace"]).exists()


def test_read_only_profile(agent_gw):
    client, store = agent_gw()
    raw, _ = store.create("r", agent_profile="ro")
    SCRIPT.extend([{"tool_calls": [tool_call("c1", "shell", command="ls")]}, {"content": "нет shell"}])
    with client:
        sid = client.post("/agent/sessions", json={"model": "alpha"}, headers=auth(raw)).json()["id"]
        evs = send(client, raw, sid, "x")
        names = [t["name"] for t in client.get("/agent/tools", headers=auth(raw)).json()["data"]]
    assert names == ["read_file"]
    assert "нет инструмента 'shell'" in next(e for e in evs if e["type"] == "tool_result")["content"]


def test_upstream_not_configured(agent_gw):
    client, store = agent_gw(client_cert="/nonexistent.crt", client_key="/nonexistent.key")
    raw, _ = store.create("n", agent_profile="default")
    with client:
        sid = client.post("/agent/sessions", json={"model": "alpha"}, headers=auth(raw)).json()["id"]
        r = client.post(f"/agent/sessions/{sid}/messages", json={"content": "x"}, headers=auth(raw))
    assert r.status_code == 503 and r.json()["error"]["code"] == "gateway_not_configured"


def test_chat_page_has_agent_mode(make):
    client, _ = make()
    with client:
        page = client.get("/chat/").text
    assert '<option value="agent">Агент</option>' in page and "/agent/sessions" in page
