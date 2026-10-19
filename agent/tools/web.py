"""Сетевые инструменты: web_fetch, http_request, web_search.

Перед каждым запросом (и каждым редиректом) хост проверяется: схема http/https, списки
доменов из настроек, все адреса, в которые он резолвится, должны быть публичными
(иначе — `network.allow_private` или `network.allow_hosts`). Подробности — docs/wiki/tools/web.md.
"""

import asyncio
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from ..permissions import host_matches
from .base import Tool, ToolContext, ToolError, ToolResult

TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml",
              "application/javascript", "application/rss+xml", "application/atom+xml")
DEFAULT_MAX_CHARS = 20000


# ---------- защита от SSRF ----------


def _bad_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


async def check_url(url: str, net: dict) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolError(f"разрешены только http/https, а не {parts.scheme or 'пусто'}")
    host = (parts.hostname or "").lower()
    if not host:
        raise ToolError("в URL нет хоста")
    if any(host_matches(host, d) for d in net["deny_domains"]):
        raise ToolError(f"домен {host} запрещён настройками (network.deny_domains)")
    if net["allow_domains"] and not any(host_matches(host, d) for d in net["allow_domains"]):
        raise ToolError(f"домен {host} не входит в network.allow_domains")
    if net["allow_private"] or host in net["allow_hosts"]:
        return
    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ToolError(f"не удалось найти хост {host}: {exc.strerror or exc}") from None
        addrs = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos}
    bad = sorted(str(a) for a in addrs if _bad_ip(a))
    if bad:
        raise ToolError(f"{host} → {', '.join(bad)}: внутренние адреса запрещены "
                        "(network.allow_private / network.allow_hosts)")


def http_client(net: dict) -> httpx.AsyncClient:
    # Как и шлюз: системные HTTP(S)_PROXY не используем, прокси — только явный.
    return httpx.AsyncClient(timeout=httpx.Timeout(net["timeout"]), follow_redirects=False,
                             trust_env=False, proxy=net["proxy"] or None,
                             headers={"user-agent": net["user_agent"]})


async def request(ctx: ToolContext, method: str, url: str, *, headers: dict | None = None,
                  content: bytes | None = None, data: dict | None = None) -> tuple[httpx.Response, bytes, bool]:
    """Запрос с проверкой каждого редиректа; тело читается не больше network.max_bytes."""
    net = ctx.settings["network"]
    async with http_client(net) as client:
        for _ in range(net["max_redirects"] + 1):
            await check_url(url, net)
            req = client.build_request(method, url, headers=headers, content=content, data=data)
            try:
                resp = await client.send(req, stream=True)
            except httpx.TimeoutException:
                raise ToolError(f"таймаут {net['timeout']} с: {url}") from None
            except httpx.HTTPError as exc:
                raise ToolError(f"ошибка соединения с {url}: {type(exc).__name__}: {exc}") from None
            try:
                if resp.is_redirect and "location" in resp.headers:
                    url = urljoin(str(resp.url), resp.headers["location"])
                    if resp.status_code in (301, 302, 303) and method not in ("GET", "HEAD"):
                        method, content, data = "GET", None, None
                    continue
                body, cut = bytearray(), False
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > net["max_bytes"]:
                        del body[net["max_bytes"]:]
                        cut = True
                        break
                return resp, bytes(body), cut
            finally:
                await resp.aclose()
        raise ToolError(f"больше {net['max_redirects']} редиректов")


# ---------- HTML → текст ----------


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "object", "canvas"}
    BLOCK = {"p", "div", "section", "article", "header", "footer", "nav", "aside", "main", "table",
             "tr", "ul", "ol", "dl", "dt", "dd", "blockquote", "form", "figure", "figcaption", "hr", "br"}

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base, self.out, self.title = base_url, [], ""
        self.skip = 0
        self.in_title = self.pre = False
        self.link: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self.in_title = True
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "pre":
            self.pre = True
            self.out.append("\n```\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self.link = (urljoin(self.base, href), [])

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self.in_title = False
        elif tag == "pre":
            self.pre = False
            self.out.append("\n```\n")
        elif re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCK:
            self.out.append("\n")
        elif tag == "a" and self.link:
            href, parts = self.link
            self.link = None
            text = " ".join("".join(parts).split())
            if text:
                self.out.append(f"[{text}]({href})")

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title += data
            return
        if self.link is not None:
            self.link[1].append(data)
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        raw = "".join(self.out)
        lines = [line.rstrip() for line in raw.split("\n")]
        text = "\n".join(lines)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_text(html: str, base_url: str = "") -> tuple[str, str]:
    """→ (заголовок, текст в духе markdown: заголовки #, списки -, ссылки [текст](url))."""
    parser = _TextExtractor(base_url)
    parser.feed(html)
    parser.close()
    return " ".join(parser.title.split()), parser.text()


def decode(body: bytes, resp: httpx.Response) -> str:
    charset = resp.charset_encoding
    if not charset:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
        charset = m.group(1).decode() if m else "utf-8"
    try:
        return body.decode(charset, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def page(text: str, offset: int, max_chars: int) -> str:
    chunk = text[offset:offset + max_chars]
    end = offset + len(chunk)
    if offset or end < len(text):
        chunk += f"\n\n…(символы {offset}–{end} из {len(text)}" + (
            f"; продолжение: offset={end})" if end < len(text) else ")")
    return chunk


# ---------- инструменты ----------


class UrlTool(Tool):
    def subject(self, args: dict) -> str | None:
        return args.get("url")


class WebFetch(UrlTool):
    name = "web_fetch"
    description = ("Скачать страницу по URL (GET) и вернуть её текст: HTML превращается в текст с "
                   "заголовками, списками и ссылками [текст](url). Длинные страницы — частями через offset.")
    parameters = {"type": "object", "properties": {
        "url": {"type": "string", "minLength": 1},
        "offset": {"type": "integer", "minimum": 0, "description": "с какого символа текста"},
        "max_chars": {"type": "integer", "minimum": 100, "description": f"по умолчанию {DEFAULT_MAX_CHARS}"},
        "raw": {"type": "boolean", "description": "вернуть HTML как есть"},
    }, "required": ["url"], "additionalProperties": False}

    async def run(self, args, ctx):
        resp, body, cut = await request(ctx, "GET", args["url"])
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype and not ctype.startswith(TEXT_TYPES):
            raise ToolError(f"{resp.status_code}: не текст ({ctype}, {len(body)} байт)")
        text, title = decode(body, resp), ""
        if not args.get("raw") and (ctype in ("text/html", "application/xhtml+xml") or
                                    (not ctype and b"<html" in body[:2000].lower())):
            title, text = html_to_text(text, str(resp.url))
        head = [f"URL: {resp.url}", f"Статус: {resp.status_code}", f"Тип: {ctype or '?'}"]
        if title:
            head.append(f"Заголовок: {title}")
        if cut:
            head.append(f"(скачаны первые {len(body)} байт — network.max_bytes)")
        body_text = page(text, args.get("offset", 0), args.get("max_chars", DEFAULT_MAX_CHARS))
        return ToolResult("\n".join(head) + "\n\n" + body_text, is_error=resp.status_code >= 400)


class HttpRequest(UrlTool):
    name = "http_request"
    description = ("HTTP-запрос к API: любой метод, заголовки, тело (строка) или json (объект). "
                   "Возвращает статус, заголовки ответа и тело.")
    parameters = {"type": "object", "properties": {
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]},
        "url": {"type": "string", "minLength": 1},
        "headers": {"type": "object", "additionalProperties": {"type": "string"}},
        "body": {"type": "string"},
        "json": {"description": "тело как JSON (вместо body)"},
    }, "required": ["method", "url"], "additionalProperties": False}

    async def run(self, args, ctx):
        headers = dict(args.get("headers") or {})
        content = None
        if "json" in args:
            content = json.dumps(args["json"], ensure_ascii=False).encode()
            headers.setdefault("content-type", "application/json")
        elif "body" in args:
            content = args["body"].encode()
        resp, body, cut = await request(ctx, args["method"], args["url"], headers=headers, content=content)
        lines = [f"HTTP {resp.status_code} {resp.reason_phrase}", f"URL: {resp.url}"]
        lines += [f"{k}: {v}" for k, v in resp.headers.items() if k.lower() not in ("set-cookie",)]
        text = decode(body, resp)
        ctype = resp.headers.get("content-type", "")
        if body and not ctype.startswith(TEXT_TYPES) and b"\0" in body[:1024]:
            text = f"(двоичное тело, {len(body)} байт)"
        if cut:
            text += f"\n…(тело обрезано до {len(body)} байт — network.max_bytes)"
        return "\n".join(lines) + "\n\n" + text


class _DDGParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self.field: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "a" and "result__a" in cls:
            self.results.append({"title": "", "url": _ddg_url(a.get("href") or ""), "snippet": ""})
            self.field = "title"
        elif "result__snippet" in cls and self.results:
            self.field = "snippet"

    def handle_endtag(self, tag):
        if tag in ("a", "td", "div"):
            self.field = None

    def handle_data(self, data):
        if self.field and self.results:
            self.results[-1][self.field] += data


def _ddg_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg")
        if target:
            return target[0]
    return href


def parse_duckduckgo(html: str) -> list[dict]:
    parser = _DDGParser()
    parser.feed(html)
    out = []
    for r in parser.results:
        url = r["url"]
        if not url.startswith("http") or "duckduckgo.com/y.js" in url:  # реклама
            continue
        out.append({"title": " ".join(r["title"].split()), "url": url, "snippet": " ".join(r["snippet"].split())})
    return out


class WebSearch(Tool):
    name = "web_search"
    default_permission = "allow"
    description = ("Поиск в интернете. Возвращает список: заголовок, URL, фрагмент. "
                   "Чтобы прочитать страницу, вызови web_fetch с её URL.")
    parameters = {"type": "object", "properties": {
        "query": {"type": "string", "minLength": 1},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 30},
    }, "required": ["query"], "additionalProperties": False}

    def subject(self, args: dict) -> str | None:
        return args.get("query")

    async def run(self, args, ctx):
        cfg = ctx.settings["search"]
        limit = args.get("max_results") or cfg["max_results"]
        if cfg["provider"] == "searxng":
            if not cfg["searxng_url"]:
                raise ToolError("search.provider = searxng, но не задан search.searxng_url")
            url = urljoin(cfg["searxng_url"].rstrip("/") + "/", "search")
            resp, body, _ = await request(ctx, "GET", str(httpx.URL(url, params={"q": args["query"], "format": "json"})))
            if resp.status_code != 200:
                raise ToolError(f"SearXNG ответил {resp.status_code} (включён ли формат json в settings.yml?)")
            try:
                items = json.loads(body).get("results", [])
            except ValueError:
                raise ToolError("SearXNG вернул не JSON") from None
            results = [{"title": i.get("title", ""), "url": i.get("url", ""), "snippet": i.get("content", "")}
                       for i in items]
        else:
            resp, body, _ = await request(ctx, "POST", cfg["duckduckgo_url"], data={"q": args["query"]})
            html = decode(body, resp)
            results = parse_duckduckgo(html)
            if not results and ("anomaly" in html or resp.status_code in (202, 403, 429)):
                raise ToolError(f"DuckDuckGo не отдал результаты (статус {resp.status_code}, вероятно, "
                                "защита от роботов); попробуй позже или настрой search.provider = searxng")
        if not results:
            return f"По запросу «{args['query']}» ничего не найдено"
        lines = [f"{n}. {r['title']}\n   {r['url']}" + (f"\n   {r['snippet']}" if r["snippet"] else "")
                 for n, r in enumerate(results[:limit], 1)]
        return "\n".join(lines)


TOOLS = [WebFetch, HttpRequest, WebSearch]
