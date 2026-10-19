"""Шлюз против настоящего mTLS-апстрима (uvicorn с CERT_REQUIRED). Фикстуры — в conftest.py."""

import json
import time

from tests.fakes import SEEN, auth


CHAT = {"model": "alpha", "messages": [{"role": "user", "content": "hi"}]}


def test_no_token(make):
    client, _ = make()
    with client:
        r = client.post("/v1/chat/completions", json=CHAT)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_api_key"


def test_bad_revoked_expired(make):
    client, store = make()
    revoked, t = store.create("rev")
    store.revoke(t.id)
    expired, _ = store.create("old", days=1)
    store.db.execute("UPDATE tokens SET expires=? WHERE name='old'", (time.time() - 1,))
    with client:
        for raw in ("gw-nope", revoked, expired):
            assert client.post("/v1/chat/completions", json=CHAT, headers=auth(raw)).status_code == 401
    assert SEEN == []


def test_chat_through_mtls(make):
    client, store = make()
    raw, t = store.create("alice")
    with client:
        r = client.post("/v1/chat/completions", json=CHAT, headers=auth(raw))
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "привет"
    # токен пользователя наверх не уходит
    assert SEEN[-1]["auth"] is None
    row = store.usage(t.id)[0]
    assert (row["requests"], row["prompt_tokens"], row["completion_tokens"]) == (1, 7, 3)


def test_x_api_key_and_upstream_key(make):
    client, store = make(upstream_api_key="sk-upstream")
    raw, _ = store.create("bob")
    with client:
        r = client.post("/v1/embeddings", json={"model": "alpha", "input": "x"}, headers={"x-api-key": raw})
    assert r.status_code == 200
    with client:
        client.get("/v1/models", headers=auth(raw))
    assert SEEN[-1]["auth"] == "Bearer sk-upstream"


def test_stream(make):
    client, store = make()
    raw, t = store.create("carol")
    with client:
        with client.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}, headers=auth(raw)) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            text = "".join(r.iter_text())
    events = [line[6:] for line in text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    words = [c["delta"]["content"] for e in events[:-1] for c in json.loads(e)["choices"]]
    assert "".join(words) == "привет"
    assert SEEN[-1]["body"]["stream_options"]["include_usage"] is True
    row = store.usage(t.id)[0]
    assert (row["prompt_tokens"], row["completion_tokens"]) == (7, 3)


def test_model_allowlist(make):
    client, store = make()
    raw, _ = store.create("dave", models=["beta"])
    with client:
        r = client.post("/v1/chat/completions", json=CHAT, headers=auth(raw))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "model_not_allowed"
        ids = [m["id"] for m in client.get("/v1/models", headers=auth(raw)).json()["data"]]
    assert ids == ["beta"]


def test_rpm_limit(make):
    client, store = make()
    raw, _ = store.create("eve", rpm=2)
    with client:
        codes = [client.get("/v1/models", headers=auth(raw)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_body_limit_and_unknown_path(make):
    client, store = make(max_request_body_bytes=100)
    raw, _ = store.create("frank")
    with client:
        big = {**CHAT, "messages": [{"role": "user", "content": "x" * 500}]}
        assert client.post("/v1/chat/completions", json=big, headers=auth(raw)).status_code == 413
        assert client.post("/v1/files", json={}, headers=auth(raw)).status_code == 404


def test_without_client_cert_upstream_rejects(make):
    """Доказывает, что апстрим действительно требует сертификат, а шлюз его подставляет."""
    client, store = make(client_cert="", client_key="")
    raw, _ = store.create("gina")
    with client:
        r = client.post("/v1/chat/completions", json=CHAT, headers=auth(raw))
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "upstream_error"


def test_health_and_chat_page(make):
    client, _ = make()
    with client:
        assert client.get("/health").json() == {"status": "ok", "upstream": True}
        page = client.get("/chat/")
    assert page.status_code == 200 and "LLM Чат" in page.text


def test_missing_cert_files_give_503_not_crash(make):
    client, store = make(client_cert="/nonexistent.crt", client_key="/nonexistent.key")
    raw, _ = store.create("hank")
    with client:
        assert client.get("/health").status_code == 503
        r = client.post("/v1/chat/completions", json=CHAT, headers=auth(raw))
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "gateway_not_configured"
