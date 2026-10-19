"""Проверки оценщиков агентских задач, включая неверные ответы модели."""

import json
import re

import pytest

from bench.client import Completion
from bench.runner import Context, build_config
from bench.suites import BENCHES
from bench.suites.agent_tasks import strict_json


def call(name, args, id="call-1"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Model:
    def __init__(self, fault=""):
        self.fault = fault
        self.requests = []

    async def complete(self, model, messages, **kwargs):
        # Сохраняем снимок: последующие ходы не должны менять предыдущий запрос.
        self.requests.append(json.loads(json.dumps(messages)))
        prompt = messages[0]["content"]
        if self.fault == "http":
            return Completion(status=429, error="rate limit")
        if prompt.startswith("Верни только JSON"):
            data = json.loads(prompt.split("Данные: ")[1].split("\nСхема:")[0])
            if self.fault == "schema":
                data["extra"] = True
            if self.fault == "value":
                if "count" in data:
                    data["count"] += 1
                else:
                    data["items"][0]["quantity"] += 1
            text = json.dumps(data, ensure_ascii=False)
            if self.fault == "markdown":
                text = '```json\n' + text + '\n```'
            return Completion(status=200, text=text)
        if prompt.startswith("Найди товар"):
            if len(messages) == 1:
                calls = [call("find_product", {"name": re.search('«(.+?)»', prompt)[1]})]
            elif len(messages) == 3:
                assert messages[-1]["tool_call_id"] == messages[-2]["tool_calls"][0]["id"]
                product_id = json.loads(messages[-1]["content"])["product_id"]
                calls = [call("get_price", {"product_id": product_id}, id="call-2")]
                if self.fault == "invented_id":
                    calls[0]["function"]["arguments"] = '{"product_id":"invented"}'
                if self.fault == "duplicate_id":
                    calls[0]["id"] = "call-1"
            else:
                assert messages[-1]["role"] == "tool"
                price = json.loads(messages[-1]["content"])["price"]
                return Completion(status=200, text=str(price + (self.fault == "wrong_price")))
            if self.fault == "early":
                return Completion(status=200, text="1234")
            return Completion(status=200, tool_calls=calls)
        if "ПРИВЕТ" in prompt:
            return Completion(status=200, text="ПРИВЕТ", tool_calls=
                              [call("weather", {"city": "Омск"})] if self.fault == "unneeded" else [])
        if "weather" in prompt:
            calls = [call("weather", {"city": prompt.split("город ")[1].split(".")[0]})]
        else:
            a, b = map(int, re.search(r"(\d+) \* (\d+)", prompt).groups())
            calls = [call("multiply", {"a": a, "b": b})]
        if self.fault == "wrong_tool":
            calls[0]["function"]["name"] = "other"
        if self.fault == "bad_json":
            calls[0]["function"]["arguments"] = "{"
        if self.fault == "multiple":
            calls *= 2
        return Completion(status=200, tool_calls=calls)


async def run(name, fault=""):
    model = Model(fault)
    result = await BENCHES[name].run(Context(model, "fake", build_config(BENCHES, quick=True)))
    return result, model


@pytest.mark.parametrize("name,cases", [("tools", 3), ("structured", 2), ("chain", 1)])
async def test_success(name, cases):
    result, model = await run(name)
    assert result.score == 100 and result.metrics["passed"] == cases
    if name == "chain":
        assert [len(m) for m in model.requests] == [1, 3, 5]
        assert len(result.details[0]["trace"]) == 3


@pytest.mark.parametrize("name,fault", [
    ("tools", "wrong_tool"), ("tools", "bad_json"), ("tools", "multiple"), ("tools", "unneeded"),
    ("structured", "schema"), ("structured", "value"), ("structured", "markdown"),
    ("chain", "invented_id"), ("chain", "duplicate_id"), ("chain", "early"), ("chain", "wrong_price"),
    ("tools", "http"), ("structured", "http"), ("chain", "http")])
async def test_failure(name, fault):
    result, _ = await run(name, fault)
    assert result.score < 100
    assert any(r["error"] for r in result.details)
    if fault == "value":
        assert result.metrics["schema_valid"] == 2 and result.score == 0


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_nonstandard_json(text):
    with pytest.raises(ValueError):
        strict_json(text)


async def test_chain_over_http():
    import httpx
    from bench.client import BenchClient
    model = Model()
    async def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert {t["function"]["name"] for t in body["tools"]} == {"find_product", "get_price"}
        result = await model.complete(body["model"], body["messages"])
        return httpx.Response(200, json={"choices": [{"message": {
            "content": result.text, "tool_calls": result.tool_calls}}]})
    client = BenchClient("http://test/v1", transport=httpx.MockTransport(handler))
    try:
        result = await BENCHES["chain"].run(Context(client, "fake", build_config(BENCHES, quick=True)))
        assert result.score == 100 and len(model.requests) == 3
    finally:
        await client.close()
