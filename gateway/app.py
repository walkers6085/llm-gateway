"""LLM Gateway: OpenAI-совместимый шлюз к инференсу за mTLS.

Клиентский сертификат живёт только здесь; пользователи ходят по личным
токенам `gw-…` (выдаёт `gwtoken`). Ответы апстрима проксируются как есть,
стриминг SSE — без пересборки.
"""

import asyncio
import json
import logging
import ssl
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT, Settings
from .tokens import Token, TokenStore

log = logging.getLogger("gateway")

PROXIED_POST = {"chat/completions", "completions", "embeddings"}
# Заголовки ответа апстрима, которые не пробрасываем (их выставит наш сервер).
HOP_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding", "keep-alive"}


class GatewayError(Exception):
    def __init__(self, status: int, message: str, type_: str = "invalid_request_error", code: str | None = None):
        self.status, self.message, self.type, self.code = status, message, type_, code


def error_body(message: str, type_: str, code: str | None = None) -> dict:
    return {"error": {"message": message, "type": type_, "code": code, "param": None}}


def build_ssl_context(settings: Settings) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=settings.ca_bundle or None)
    if settings.client_cert:
        ctx.load_cert_chain(
            settings.client_cert,
            settings.client_key or None,
            password=settings.client_key_password or None,
        )
    return ctx


def check_cert_expiry(path: str) -> None:
    if not path:
        log.warning("CLIENT_CERT не задан — запросы к апстриму пойдут без клиентского сертификата")
        return
    try:
        from cryptography import x509

        with open(path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        left = (cert.not_valid_after_utc - datetime.now(timezone.utc)).days
        if left < 0:
            log.error("Клиентский сертификат %s истёк %s", path, cert.not_valid_after_utc.date())
        elif left < 14:
            log.warning("Клиентский сертификат %s истекает через %d дн.", path, left)
        else:
            log.info("Клиентский сертификат: %s, действует до %s", cert.subject.rfc4514_string(),
                     cert.not_valid_after_utc.date())
    except Exception as exc:  # не мешаем старту, но пишем в лог
        log.warning("Не удалось прочитать срок сертификата %s: %s", path, exc)


class RateLimiter:
    """Скользящее окно в 60 с на токен (в памяти процесса)."""

    def __init__(self):
        self.hits: dict[int, deque] = defaultdict(deque)

    def allow(self, token: Token) -> bool:
        if token.rpm <= 0:
            return True
        now = time.monotonic()
        q = self.hits[token.id]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= token.rpm:
            return False
        q.append(now)
        return True


class ModelLimiter:
    """Лимит одновременных запросов к апстриму на модель (как MODEL_MAX_CONNECTIONS в gpt2giga)."""

    def __init__(self, per_model: dict[str, int], default: int):
        self.per_model, self.default = per_model, default
        self.sems: dict[str, asyncio.Semaphore] = {}

    def get(self, model: str | None) -> asyncio.Semaphore | None:
        limit = self.per_model.get(model or "", self.default)
        if limit <= 0:
            return None
        key = model or ""
        if key not in self.sems:
            self.sems[key] = asyncio.Semaphore(limit)
        return self.sems[key]


def extract_usage(payload: dict) -> tuple[int, int]:
    usage = payload.get("usage") or {}
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


def create_app(settings: Settings | None = None, store: TokenStore | None = None,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or TokenStore(settings.db_path)
    rate = RateLimiter()
    models_limit = ModelLimiter(settings.model_max_connections, settings.model_max_connections_default)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config_error = None
        if transport:
            kwargs = {"transport": transport}
        else:
            check_cert_expiry(settings.client_cert)
            try:
                kwargs = {"verify": build_ssl_context(settings)}
            except (OSError, ssl.SSLError) as exc:
                # Не уходим в цикл рестартов: шлюз поднимается и отвечает 503 с причиной.
                app.state.config_error = f"не удалось загрузить сертификат/ключ/CA: {exc}"
                log.error("Шлюз не настроен: %s", app.state.config_error)
                kwargs = {}
        if not settings.upstream_base_url:
            app.state.config_error = app.state.config_error or "не задан UPSTREAM_BASE_URL"
        app.state.client = httpx.AsyncClient(
            base_url=settings.upstream_base_url,
            timeout=httpx.Timeout(settings.upstream_timeout, connect=15),
            **kwargs,
        )
        yield
        await app.state.client.aclose()

    app = FastAPI(
        title="LLM Gateway",
        lifespan=lifespan,
        docs_url=None if settings.prod else "/docs",
        redoc_url=None,
        openapi_url=None if settings.prod else "/openapi.json",
    )
    app.state.settings, app.state.store = settings, store
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                           allow_methods=["GET", "POST", "OPTIONS"],
                           allow_headers=["authorization", "content-type", "x-api-key"])

    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError):
        return JSONResponse(error_body(exc.message, exc.type, exc.code), status_code=exc.status)

    def authenticate(request: Request) -> Token:
        raw = ""
        auth = request.headers.get("authorization", "")
        if auth[:7].lower() == "bearer ":
            raw = auth[7:].strip()
        raw = raw or request.headers.get("x-api-key", "").strip()
        if not raw:
            raise GatewayError(401, "Нужен токен: Authorization: Bearer gw-…", "authentication_error", "missing_api_key")
        token = store.lookup(raw)
        if token is None or not token.active:
            raise GatewayError(401, "Токен недействителен, отозван или истёк", "authentication_error", "invalid_api_key")
        if not rate.allow(token):
            raise GatewayError(429, f"Превышен лимит {token.rpm} запросов в минуту", "rate_limit_error", "rate_limit_exceeded")
        return token

    def upstream() -> httpx.AsyncClient:
        if app.state.config_error:
            raise GatewayError(503, "Шлюз не настроен: " + app.state.config_error, "upstream_error", "gateway_not_configured")
        return app.state.client

    def upstream_headers() -> dict[str, str]:
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        if settings.upstream_api_key:
            headers["authorization"] = f"Bearer {settings.upstream_api_key}"
        return headers

    def upstream_failure(exc: Exception) -> GatewayError:
        if isinstance(exc, httpx.TimeoutException):
            return GatewayError(504, "Апстрим не ответил вовремя", "upstream_error", "upstream_timeout")
        cause = exc.__cause__ or exc.__context__
        if isinstance(exc, ssl.SSLError) or isinstance(cause, ssl.SSLError) or "SSL" in str(exc):
            log.error("TLS-ошибка при обращении к апстриму: %s", exc)
            return GatewayError(502, "Ошибка TLS при подключении к апстриму (сертификат шлюза или CA)",
                                "upstream_error", "upstream_tls_error")
        log.error("Апстрим недоступен: %r", exc)
        return GatewayError(502, "Апстрим недоступен", "upstream_error", "upstream_unavailable")

    def passthrough_headers(resp: httpx.Response) -> dict[str, str]:
        return {k: v for k, v in resp.headers.items() if k.lower() not in HOP_HEADERS}

    async def read_body(request: Request) -> dict:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_request_body_bytes:
            raise GatewayError(413, "Слишком большой запрос", code="request_too_large")
        raw = await request.body()
        if len(raw) > settings.max_request_body_bytes:
            raise GatewayError(413, "Слишком большой запрос", code="request_too_large")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise GatewayError(400, "Тело запроса должно быть JSON")
        if not isinstance(body, dict):
            raise GatewayError(400, "Тело запроса должно быть JSON-объектом")
        return body

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/chat/")

    @app.get("/health")
    async def health():
        if app.state.config_error:
            return JSONResponse({"status": "degraded", "upstream": "not_configured"}, status_code=503)
        try:
            resp = await app.state.client.get("/models", headers=upstream_headers(), timeout=10)
            ok = resp.status_code < 500
        except Exception as exc:
            log.warning("health: апстрим недоступен: %r", exc)
            ok = False
        return JSONResponse({"status": "ok" if ok else "degraded", "upstream": ok},
                            status_code=200 if ok else 503)

    @app.get("/v1/models")
    async def list_models(token: Token = Depends(authenticate)):
        started = time.monotonic()
        try:
            resp = await upstream().get("/models", headers=upstream_headers())
        except httpx.HTTPError as exc:
            raise upstream_failure(exc)
        ms = int((time.monotonic() - started) * 1000)
        store.record(token.id, "models", None, resp.status_code, ms=ms)
        if resp.status_code != 200:
            return Response(resp.content, status_code=resp.status_code, headers=passthrough_headers(resp))
        payload = resp.json()
        if token.models and isinstance(payload.get("data"), list):
            payload["data"] = [m for m in payload["data"] if m.get("id") in token.models]
        return payload

    @app.post("/v1/{path:path}")
    async def proxy(path: str, request: Request, token: Token = Depends(authenticate)):
        if path not in PROXIED_POST:
            raise GatewayError(404, f"Эндпоинт /v1/{path} не поддерживается", code="not_found")
        client = upstream()
        body = await read_body(request)
        model = body.get("model")
        if not token.allows(model):
            raise GatewayError(403, f"Модель {model!r} недоступна для этого токена", "permission_error", "model_not_allowed")

        stream = bool(body.get("stream"))
        if stream and path == "chat/completions":
            opts = body.get("stream_options") or {}
            opts.setdefault("include_usage", True)
            body["stream_options"] = opts

        sem = models_limit.get(model)
        if sem is not None:
            await sem.acquire()
        started = time.monotonic()
        upstream_req = client.build_request("POST", "/" + path, json=body, headers=upstream_headers())
        try:
            resp = await client.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            if sem is not None:
                sem.release()
            store.record(token.id, path, model, 502, ms=int((time.monotonic() - started) * 1000))
            raise upstream_failure(exc)

        def finish(status: int, usage: tuple[int, int]) -> None:
            if sem is not None:
                sem.release()
            store.record(token.id, path, model, status, usage[0], usage[1],
                         int((time.monotonic() - started) * 1000))

        is_sse = resp.headers.get("content-type", "").startswith("text/event-stream")
        if not (stream and is_sse and resp.status_code == 200):
            try:
                content = await resp.aread()
            except httpx.HTTPError as exc:
                finish(502, (0, 0))
                raise upstream_failure(exc)
            finally:
                await resp.aclose()
            usage = (0, 0)
            if resp.status_code == 200:
                try:
                    usage = extract_usage(json.loads(content))
                except (ValueError, AttributeError):
                    pass
            finish(resp.status_code, usage)
            return Response(content, status_code=resp.status_code, headers=passthrough_headers(resp))

        async def relay():
            usage = (0, 0)
            status = resp.status_code
            tail = b""
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
                    # Ищем usage в последних событиях, не меняя поток.
                    tail += chunk
                    *lines, tail = tail.split(b"\n")
                    for line in lines:
                        if line.startswith(b"data:") and b'"usage"' in line:
                            try:
                                found = extract_usage(json.loads(line[5:].strip()))
                                if any(found):
                                    usage = found
                            except (ValueError, AttributeError):
                                pass
            except httpx.HTTPError as exc:
                status = 502
                log.error("Обрыв стрима от апстрима: %r", exc)
            except asyncio.CancelledError:
                status = 499  # клиент закрыл соединение
                raise
            finally:
                await resp.aclose()
                finish(status, usage)

        headers = passthrough_headers(resp)
        headers["cache-control"] = "no-cache"
        headers["x-accel-buffering"] = "no"
        return StreamingResponse(relay(), status_code=200, headers=headers, media_type="text/event-stream")

    chat_dir = ROOT / "chat"
    if chat_dir.is_dir():
        app.mount("/chat", StaticFiles(directory=chat_dir, html=True), name="chat")
    return app
