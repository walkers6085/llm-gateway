"""Офлайн-проверки измерений; реальные модели здесь не вызываются."""

import json

import httpx
import pytest

from bench.client import BenchClient, Completion
from bench.runner import Context, build_config, make_report, run_all, save_report
from bench.suites import BENCHES


async def test_client_stream_and_url():
    def handler(req):
        assert req.url.path == "/v1/chat/completions"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=
            'data: {"choices":[{"delta":{"content":"Привет"}}]}\n\n'
            'data: {"usage":{"prompt_tokens":10,"completion_tokens":2},"choices":[]}\n\n'
            'data: [DONE]\n\n')
    client = BenchClient("http://test/v1", transport=httpx.MockTransport(handler))
    try:
        r = await client.complete("test", [])
        assert r.ok and r.text == "Привет" and r.usage_reported
        assert r.prompt_tokens == 10 and r.completion_tokens == 2
        assert r.ttft is not None
    finally:
        await client.close()


def test_config_and_report(tmp_path):
    cfg = build_config(BENCHES, quick=True, sets=["speed.repeats=3"])
    assert cfg["speed"]["repeats"] == 3
    with pytest.raises(ValueError):
        build_config(BENCHES, sets=["speed.repeats=0"])
    with pytest.raises(ValueError):
        build_config(BENCHES, sets=["speed.repeats.x=1"])
    with pytest.raises(ValueError):
        build_config(BENCHES, sets=["needle.depths=[]"])
    report = make_report("model.v1", "http://test", cfg, [], {})
    jp, mp = save_report(report, BENCHES, tmp_path)
    assert "model.v1" in jp.name and json.loads(jp.read_text())["model"] == "model.v1"
    assert mp.exists()


class Fake:
    def __init__(self, mode="correct"):
        self.mode = mode

    async def complete(self, model, messages, **kwargs):
        import re
        text = messages[0]["content"]
        if self.mode == "network":
            return Completion(error="offline")
        code = re.search(r"Альтаир: ([a-f0-9]+)", text)
        arithmetic = re.search(r"\((\d+) \+ (\d+)\) \* (\d+)", text)
        answer = code[1] if code else str((int(arithmetic[1]) + int(arithmetic[2])) * int(arithmetic[3])) if arithmetic else "ОК"
        return Completion(status=200, text=answer, prompt_tokens=max(1, len(text)//4),
                          completion_tokens=2, usage_reported=True, ttft=.1, total=.2)


async def test_suites_and_errors():
    cfg = build_config(BENCHES, quick=True)
    results = await run_all(Context(Fake(), "fake", cfg), [BENCHES[n] for n in ("speed", "context", "needle", "reasoning")])
    assert not any(r.error for r in results)
    assert all(r.score == 100 for r in results if r.quality)
    assert results[1].metrics["ceiling_reached"]
    ctx = Context(Fake("network"), "fake", cfg)
    ctx.shared["cpt_ru"] = 4
    r = await BENCHES["context"].run(ctx)
    assert r.error == "offline" and "context_tokens" not in ctx.shared


async def test_context_rejection_boundary():
    class Limited(Fake):
        async def complete(self, model, messages, **kwargs):
            if len(messages[0]["content"]) > 10000:
                return Completion(status=400, error="maximum context length exceeded")
            return await super().complete(model, messages, **kwargs)
    cfg = build_config(BENCHES, sets=["context.steps=8", "context.max_tokens=4096"])
    ctx = Context(Limited(), "fake", cfg)
    ctx.shared["cpt_ru"] = 4
    r = await BENCHES["context"].run(ctx)
    assert not r.error and 2000 < r.metrics["context_tokens"] < 2600


async def test_bench_provider_configuration(tmp_path, monkeypatch):
    from argparse import Namespace
    from agent.providers import ProviderManager
    from bench import __main__ as cli
    path = tmp_path / "providers.json"
    manager = ProviderManager(path)
    manager.add("local", "http://example.org/v1", model="chosen-model")
    seen = {}
    class Client(Fake):
        def __init__(self, url, **kwargs):
            super().__init__()
            seen.update(url=url, **kwargs)
        async def close(self):
            seen["closed"] = True
    monkeypatch.setattr(cli, "BenchClient", Client)
    args = Namespace(quick=True, config=None, set=[], suites="reasoning", provider="local",
                     providers_file=str(path), url=None, model=None, output=str(tmp_path / "out"))
    assert await cli.execute(args) == 0
    report = json.loads(next((tmp_path / "out").glob("*.json")).read_text())
    assert report["provider"] == "local" and report["model"] == "chosen-model"
    assert seen["url"] == "http://example.org/v1" and seen["token"] == "" and seen["closed"]
    args.url = "http://other/v1"
    with pytest.raises(ValueError, match="нельзя"):
        await cli.execute(args)
