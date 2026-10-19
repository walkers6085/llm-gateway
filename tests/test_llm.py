"""Клиент chat/completions: стрим, сборка tool_calls, ошибки. См. docs/wiki/llm-client.md."""

import json

import httpx
import pytest

from agent.llm import LLMClient, LLMError, ToolCallBuilder
from tests.fakes import scripted_transport, tool_call


async def collect(client, **kw):
    return [ev async for ev in client.stream("alpha", [{"role": "user", "content": "x"}], **kw)]


def client_for(transport):
    return LLMClient(httpx.AsyncClient(base_url="http://up/v1", transport=transport))


async def test_text_stream_and_usage():
    seen = []
    evs = await collect(client_for(scripted_transport([{"content": "раз два три"}], seen)),
                        tools=[{"type": "function"}], temperature=0.2)
    assert "".join(e["text"] for e in evs if e["type"] == "text") == "раз два три"
    final = evs[-1]
    assert final["message"] == {"role": "assistant", "content": "раз два три"}
    assert final["usage"] == (7, 3)
    assert seen[0]["stream"] is True and seen[0]["stream_options"] == {"include_usage": True}
    assert seen[0]["temperature"] == 0.2 and seen[0]["tools"]


async def test_tool_calls_assembled_from_fragments():
    reply = {"content": "", "tool_calls": [tool_call("c1", "read_file", path="a.txt"),
                                           tool_call("c2", "grep", pattern="x")]}
    final = (await collect(client_for(scripted_transport([reply]))))[-1]
    calls = final["message"]["tool_calls"]
    assert [c["id"] for c in calls] == ["c1", "c2"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.txt"}
    assert final["message"]["content"] is None
    assert final["finish_reason"] == "tool_calls"


def test_builder_missing_ids_and_dict_arguments():
    b = ToolCallBuilder()
    b.add([{"index": 0, "function": {"name": "x", "arguments": {"a": 1}}}])
    assert b.result()[0]["id"] == "call_0"
    assert json.loads(b.result()[0]["function"]["arguments"]) == {"a": 1}


async def test_http_error_is_llm_error():
    with pytest.raises(LLMError) as exc:
        await collect(client_for(scripted_transport([{"status": 403, "error": "Модель недоступна"}])))
    assert exc.value.status == 403 and "недоступна" in exc.value.message


async def test_connection_error():
    def boom(request):
        raise httpx.ConnectError("нет связи")
    with pytest.raises(LLMError) as exc:
        await collect(client_for(httpx.MockTransport(boom)))
    assert exc.value.status == 502


async def test_non_stream_fallback():
    body = {"choices": [{"message": {"role": "assistant", "content": "целиком"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
    evs = await collect(client_for(httpx.MockTransport(lambda r: httpx.Response(200, json=body))))
    assert evs[0] == {"type": "text", "text": "целиком"}
    assert evs[-1]["usage"] == (1, 2)


async def test_error_inside_stream():
    body = b'data: {"error": {"message": "overloaded"}}\n\n'
    t = httpx.MockTransport(lambda r: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}))
    with pytest.raises(LLMError, match="overloaded"):
        await collect(client_for(t))


async def test_models():
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]}))
    assert await client_for(t).models() == ["a", "b"]
