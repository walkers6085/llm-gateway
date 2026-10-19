"""Общие подделки для тестов: PKI, mTLS-апстрим со сценариями ответов, свободный порт.

Фикстуры поверх них — в conftest.py. Сценарий агента задаётся через SCRIPT:
каждый элемент — ответ ассистента `{"content": "...", "tool_calls": [{"id", "name", "arguments"}]}`,
который апстрим отдаёт на очередной запрос chat/completions (по порядку).
"""

import datetime as dt
import ipaddress
import json
import socket
import threading
import time

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

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


def make_pki(d):
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert("Test CA", "Test CA", ca_key.public_key(), ca_key, ca=True)
    srv_key = ec.generate_private_key(ec.SECP256R1())
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
SCRIPT: list[dict] = []
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
upstream = FastAPI()


@upstream.get("/v1/models")
async def _models(request: Request):
    SEEN.append({"path": "models", "auth": request.headers.get("authorization")})
    return {"object": "list", "data": [{"id": "alpha", "object": "model"}, {"id": "beta", "object": "model"}]}


def _scripted_chunks(reply: dict):
    """Ответ из SCRIPT как поток дельт: текст по словам, аргументы инструментов — в два куска."""
    content = reply.get("content") or ""
    for i, word in enumerate(content.split(" ")):
        yield {"delta": {"role": "assistant", "content": ("" if i == 0 else " ") + word}}
    for idx, call in enumerate(reply.get("tool_calls") or []):
        args = call["arguments"] if isinstance(call["arguments"], str) else json.dumps(call["arguments"])
        half = len(args) // 2
        yield {"delta": {"tool_calls": [{"index": idx, "id": call["id"], "type": "function",
                                         "function": {"name": call["name"], "arguments": args[:half]}}]}}
        yield {"delta": {"tool_calls": [{"index": idx, "function": {"arguments": args[half:]}}]}}
    yield {"delta": {}, "finish_reason": "tool_calls" if reply.get("tool_calls") else "stop"}


@upstream.post("/v1/chat/completions")
async def _chat(request: Request):
    body = await request.json()
    SEEN.append({"path": "chat", "auth": request.headers.get("authorization"), "body": body})
    if SCRIPT:
        reply = SCRIPT.pop(0)
        if reply.get("status"):
            return JSONResponse({"error": {"message": reply["error"]}}, status_code=reply["status"])

        async def scripted():
            for chunk in _scripted_chunks(reply):
                yield "data: " + json.dumps({"choices": [{"index": 0, **chunk}]}) + "\n\n"
            yield "data: " + json.dumps({"choices": [], "usage": USAGE}) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(scripted(), media_type="text/event-stream")

    if not body.get("stream"):
        return {"id": "c1", "object": "chat.completion", "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "привет"},
                             "finish_reason": "stop"}], "usage": USAGE}

    async def gen():
        for word in ["при", "вет"]:
            yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": word}}]}) + "\n\n"
        if (body.get("stream_options") or {}).get("include_usage"):
            yield "data: " + json.dumps({"choices": [], "usage": USAGE}) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@upstream.post("/v1/embeddings")
async def _emb(request: Request):
    return {"object": "list", "data": [{"embedding": [0.1, 0.2], "index": 0}],
            "usage": {"prompt_tokens": 2, "total_tokens": 2}}

# ---------- запуск ASGI-приложений в потоке ----------


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerThread:
    """uvicorn в фоновом потоке (для апстрима, шлюза в CLI-тестах, фейкового MCP по HTTP)."""

    def __init__(self, app, **config):
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                    log_level="warning", **config))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)


def auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def tool_call(id_, name, **arguments):
    return {"id": id_, "name": name, "arguments": arguments}
