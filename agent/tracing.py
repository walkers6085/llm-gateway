"""Контекст корреляции вызовов; содержимое запросов и секреты в журнал не попадают."""

from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import json
import uuid
import time

log = logging.getLogger("gwagent.trace")

@dataclass
class Trace:
    request_id: str
    span_id: str
    session_id: str
    parent_span_id: str = ""
    budget: dict = field(default_factory=lambda: {"spent": 0, "children": 0})

current: ContextVar[Trace | None] = ContextVar("agent_trace", default=None)

def identifier():
    return uuid.uuid4().hex

def record(store, event, **data):
    ctx = current.get()
    if ctx is None:
        return
    store.trace(ctx.request_id, ctx.span_id, ctx.parent_span_id, ctx.session_id, event, data)
    log.info("%s", json.dumps({"event": event, "request_id": ctx.request_id, "span_id": ctx.span_id,
                   "parent_span_id": ctx.parent_span_id, "session_id": ctx.session_id, "ts": time.time(), **data}, ensure_ascii=False))
