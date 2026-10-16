"""Шлюз против настоящего mTLS-апстрима (uvicorn с CERT_REQUIRED)."""

import datetime as dt
import json
import socket
import threading
import time

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.tokens import TokenStore

# ---------- сертификаты ----------

def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject, issuer, pubkey, signer, *, ca=False, eku=None, san=None):
    now = dt.datetime.now(dt.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(_name(subject)).issuer_name(_name(issuer))
         .public_key(pubkey).serial_number(x509.random_serial_number())
         .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if ca:
        b = b.add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
        b = b.add_extension(x509.SubjectKeyIdentifier.from_public_key(pubkey), critical=False)
    else:
        b = b.add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        b = b.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
    if san:
        b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
    return b.sign(signer, hashes.SHA256())


def _write(path, cert=None, key=None):
    data = b""
    if cert is not None:
        data += cert.public_bytes(serialization.Encoding.PEM)
    if key is not None:
        data += key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
    path.write_bytes(data)
    return str(path)


@pytest.fixture(scope="session")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert("Test CA", "Test CA", ca_key.public_key(), ca_key, ca=True)
    srv_key = ec.generate_private_key(ec.SECP256R1())
    import ipaddress
    srv = _cert("localhost", "Test CA", srv_key.public_key(), ca_key, eku=ExtendedKeyUsageOID.SERVER_AUTH,
                san=[x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    cli_key = ec.generate_private_key(ec.SECP256R1())
    cli = _cert("gateway", "Test CA", cli_key.public_key(), ca_key, eku=ExtendedKeyUsageOID.CLIENT_AUTH)
    return {
        "ca": _write(d / "ca.pem", ca),
        "server_cert": _write(d / "server.crt", srv),
        "server_key": _write(d / "server.key", key=srv_key),
        "client_cert": _write(d / "client.crt", cli),
        "client_key": _write(d / "client.key", key=cli_key),
    }

# ---------- фейковый апстрим ----------

SEEN: list[dict] = []
upstream = FastAPI()


@upstream.get("/v1/models")
async def _models(request: Request):
    SEEN.append({"path": "models", "auth": request.headers.get("authorization")})
    return {"object": "list", "data": [{"id": "alpha", "object": "model"}, {"id": "beta", "object": "model"}]}


@upstream.post("/v1/chat/completions")
async def _chat(request: Request):
    body = await request.json()
    SEEN.append({"path": "chat", "auth": request.headers.get("authorization"), "body": body})
    usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    if not body.get("stream"):
        return {"id": "c1", "object": "chat.completion", "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "привет"},
                             "finish_reason": "stop"}], "usage": usage}

    async def gen():
        for word in ["при", "вет"]:
            yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": word}}]}) + "\n\n"
        if (body.get("stream_options") or {}).get("include_usage"):
            yield "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@upstream.post("/v1/embeddings")
async def _emb(request: Request):
    return {"object": "list", "data": [{"embedding": [0.1, 0.2], "index": 0}],
            "usage": {"prompt_tokens": 2, "total_tokens": 2}}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def upstream_url(pki):
    import ssl
    port = _free_port()
    config = uvicorn.Config(upstream, host="127.0.0.1", port=port, log_level="warning",
                            ssl_certfile=pki["server_cert"], ssl_keyfile=pki["server_key"],
                            ssl_ca_certs=pki["ca"], ssl_cert_reqs=ssl.CERT_REQUIRED)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"https://localhost:{port}/v1"
    server.should_exit = True
    thread.join(5)

# ---------- шлюз ----------


@pytest.fixture
def make(tmp_path, pki, upstream_url):
    def _make(**overrides):
        settings = Settings(upstream_base_url=upstream_url, client_cert=pki["client_cert"],
                            client_key=pki["client_key"], ca_bundle=[pki["ca"]],
                            db_path=str(tmp_path / "gw.db"), upstream_timeout=10)
        for k, v in overrides.items():
            setattr(settings, k, v)
        store = TokenStore(settings.db_path)
        return TestClient(create_app(settings, store)), store
    SEEN.clear()
    return _make


def auth(raw):
    return {"Authorization": f"Bearer {raw}"}


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
