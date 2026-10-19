"""Сетевые инструменты против локального HTTP-сервера (без интернета). См. docs/wiki/tools/web.md."""

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from agent.settings import DEFAULTS, merge
from agent.tools.base import ToolContext, ToolError
from agent.tools.web import HttpRequest, WebFetch, WebSearch, check_url, html_to_text, parse_duckduckgo
from tests.fakes import ServerThread

DDG = (Path(__file__).parent / "fixtures" / "ddg.html").read_text()
PAGE = """<html><head><title> Тестовая  страница </title><style>.x{}</style><script>alert(1)</script></head>
<body><nav><a href="/">Главная</a></nav><h1>Заголовок</h1><p>Первый   абзац
с переносом.</p><ul><li>раз</li><li>два</li></ul><pre>  код
   с отступом</pre><p>Ссылка: <a href="/docs?a=1">документация</a></p></body></html>"""

site = FastAPI()


@site.get("/page")
async def _page():
    return HTMLResponse(PAGE)


@site.api_route("/redirect", methods=["GET", "POST"])
async def _redirect(to: str):
    return RedirectResponse(to, status_code=302)


@site.get("/big")
async def _big():
    return Response("я" * 5000, media_type="text/plain")


@site.get("/bin")
async def _bin():
    return Response(b"\x89PNG\x00\x00", media_type="image/png")


@site.get("/missing")
async def _missing():
    return HTMLResponse("<h1>нет</h1>", status_code=404)


@site.api_route("/echo", methods=["GET", "POST", "PUT", "DELETE"])
async def _echo(request: Request):
    return JSONResponse({"method": request.method, "body": (await request.body()).decode(),
                         "x": request.headers.get("x-test"), "ct": request.headers.get("content-type")},
                        headers={"set-cookie": "secret=1", "x-reply": "ok"})


@site.post("/ddg")
async def _ddg(request: Request):
    form = (await request.body()).decode()
    return HTMLResponse(DDG if "q=" in form else "")


@site.post("/ddg-blocked")
async def _ddg_blocked():
    return HTMLResponse("<html>anomaly detected</html>", status_code=202)


@site.get("/searx/search")
async def _searx(q: str, format: str):
    return {"results": [{"title": f"про {q}", "url": "https://a.example/", "content": "фрагмент"}]}


@pytest.fixture(scope="module")
def base():
    with ServerThread(site) as srv:
        yield f"http://127.0.0.1:{srv.port}"


def ctx_for(tmp_path, **net):
    settings = merge(DEFAULTS, {"network": {"allow_hosts": ["127.0.0.1"], **net}})
    return ToolContext(workspace=tmp_path, settings=settings)


async def test_fetch_html_to_text(base, tmp_path):
    out = await WebFetch().run({"url": base + "/page"}, ctx_for(tmp_path))
    text = out.content
    assert "Заголовок: Тестовая страница" in text and "Статус: 200" in text
    assert "# Заголовок" in text and "Первый абзац с переносом." in text
    assert "- раз\n- два" in text
    assert "  код\n   с отступом" in text
    assert f"[документация]({base}/docs?a=1)" in text
    assert "alert" not in text and ".x{}" not in text
    raw = await WebFetch().run({"url": base + "/page", "raw": True}, ctx_for(tmp_path))
    assert "<script>" in raw.content


async def test_fetch_paging_limits_and_errors(base, tmp_path):
    part = await WebFetch().run({"url": base + "/big", "max_chars": 1000, "offset": 500}, ctx_for(tmp_path))
    assert "символы 500–1500 из 5000; продолжение: offset=1500" in part.content
    cut = await WebFetch().run({"url": base + "/big"}, ctx_for(tmp_path, max_bytes=100))
    assert "скачаны первые 100 байт" in cut.content
    with pytest.raises(ToolError, match="не текст"):
        await WebFetch().run({"url": base + "/bin"}, ctx_for(tmp_path))
    missing = await WebFetch().run({"url": base + "/missing"}, ctx_for(tmp_path))
    assert missing.is_error and "Статус: 404" in missing.content


async def test_ssrf_blocked(base, tmp_path):
    strict = ctx_for(tmp_path, allow_hosts=[])
    with pytest.raises(ToolError, match="внутренние адреса запрещены"):
        await WebFetch().run({"url": base + "/page"}, strict)
    # редирект с разрешённого хоста на запрещённый тоже проверяется
    port = base.rsplit(":", 1)[1]
    with pytest.raises(ToolError, match="localhost"):
        await WebFetch().run({"url": f"{base}/redirect?to=http://localhost:{port}/page"}, ctx_for(tmp_path))
    ok = await WebFetch().run({"url": f"{base}/redirect?to=/page"}, ctx_for(tmp_path))
    assert "Заголовок: Тестовая страница" in ok.content
    assert "Статус: 200" in (await WebFetch().run({"url": base + "/page"}, ctx_for(tmp_path, allow_hosts=[], allow_private=True))).content


@pytest.mark.parametrize("url,msg", [
    ("file:///etc/passwd", "только http/https"),
    ("http://169.254.169.254/latest/meta-data", "внутренние"),
    ("http://[::ffff:127.0.0.1]/", "внутренние"),
    ("http://10.0.0.1/", "внутренние"),
    ("http://evil.test.example/", "deny_domains"),
])
async def test_check_url_rejects(url, msg):
    net = merge(DEFAULTS, {"network": {"deny_domains": ["example"]}})["network"]
    with pytest.raises(ToolError, match=msg):
        await check_url(url, net)


async def test_allow_domains(tmp_path):
    net = merge(DEFAULTS, {"network": {"allow_domains": ["python.org"], "allow_private": True}})["network"]
    await check_url("https://docs.python.org/3/", net)
    with pytest.raises(ToolError, match="allow_domains"):
        await check_url("https://example.org/", net)


async def test_http_request(base, tmp_path):
    out = await HttpRequest().run({"method": "POST", "url": base + "/echo", "json": {"a": "б"},
                                   "headers": {"x-test": "1"}}, ctx_for(tmp_path))
    assert out.startswith("HTTP 200 OK")
    assert '"method":"POST"' in out and '"x":"1"' in out and '"ct":"application/json"' in out
    assert "x-reply: ok" in out and "secret=1" not in out
    put = await HttpRequest().run({"method": "PUT", "url": base + "/echo", "body": "сырое"}, ctx_for(tmp_path))
    assert '"body":"сырое"' in put
    # 302 после POST превращается в GET
    red = await HttpRequest().run({"method": "POST", "url": f"{base}/redirect?to=/echo", "body": "x"}, ctx_for(tmp_path))
    assert '"method":"GET"' in red


def test_html_to_text_title_and_links():
    title, text = html_to_text('<title>T</title><a href="#top">наверх</a><a href="https://x.io">X</a>', "https://b/")
    assert title == "T" and "[X](https://x.io)" in text and "[наверх]" not in text


def test_parse_duckduckgo_skips_ads_and_decodes_urls():
    results = parse_duckduckgo(DDG)
    assert [r["url"] for r in results] == ["https://docs.python.org/3/library/asyncio.html",
                                           "https://realpython.com/async-io-python/"]
    assert results[0]["title"] == "asyncio — Asynchronous I/O"
    assert "concurrent code" in results[0]["snippet"]


async def test_web_search_providers(base, tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.settings["search"]["duckduckgo_url"] = base + "/ddg"
    out = await WebSearch().run({"query": "python asyncio", "max_results": 1}, ctx)
    assert out.startswith("1. asyncio") and "2." not in out
    ctx.settings["search"]["duckduckgo_url"] = base + "/ddg-blocked"
    with pytest.raises(ToolError, match="защита от роботов"):
        await WebSearch().run({"query": "x"}, ctx)
    ctx.settings["search"].update(provider="searxng", searxng_url=base + "/searx/")
    out = await WebSearch().run({"query": "котики"}, ctx)
    assert "1. про котики\n   https://a.example/\n   фрагмент" == out
    ctx.settings["search"]["searxng_url"] = ""
    with pytest.raises(ToolError, match="searxng_url"):
        await WebSearch().run({"query": "x"}, ctx)
