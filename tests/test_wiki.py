"""Вики не отстаёт от кода: всё, что можно перечислить из кода, упомянуто на своей странице,
а все относительные ссылки в вики ведут на существующие файлы. См. docs/wiki/testing.md."""

import re
from pathlib import Path

import pytest

from agent.runtime import BUILTIN
from agent.settings import HOOK_EVENTS, flat_keys
from gateway.app import create_app
from gateway.config import Settings

ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "docs" / "wiki"
PAGES = sorted(WIKI.rglob("*.md"))


def read(name: str) -> str:
    return (WIKI / name).read_text(encoding="utf-8")


def test_every_builtin_tool_documented():
    tools_text = "".join(p.read_text(encoding="utf-8") for p in (WIKI / "tools").glob("*.md"))
    missing = [name for name in BUILTIN if f"`{name}`" not in tools_text]
    assert not missing, f"нет в docs/wiki/tools/*.md: {missing}"
    perms = read("permissions.md")
    assert not [n for n in BUILTIN if f"`{n}`" not in perms], "умолчания прав перечислены не для всех инструментов"


def test_every_setting_documented():
    text = read("settings.md")
    missing = [key for key in flat_keys() if f"`{key}`" not in text]
    assert not missing, f"нет в docs/wiki/settings.md: {missing}"


def test_every_hook_event_documented():
    text = read("hooks.md")
    assert not [e for e in HOOK_EVENTS if f"`{e}`" not in text]


def test_every_loop_event_documented():
    src = (ROOT / "agent" / "loop.py").read_text(encoding="utf-8")
    kinds = set(re.findall(r'"type": "(\w+)"', src)) | {"text"}
    text = read("agent-loop.md")
    assert not [k for k in sorted(kinds) if f"`{k}`" not in text]


def test_every_agent_endpoint_documented(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "gw.db"), agent_config=str(tmp_path / "none.json")))
    text = read("server-api.md")
    routes = [(m, r.path) for r in app.routes if getattr(r, "path", "").startswith("/agent")
              for m in sorted(getattr(r, "methods", []))]
    assert routes
    routes = [(m, p.replace("{session_id}", "{id}")) for m, p in routes]
    missing = [f"{m} {p}" for m, p in routes if f"`{m} {p}" not in text]
    assert not missing, f"нет в docs/wiki/server-api.md: {missing}"


def test_home_links_every_page():
    home = read("Home.md")
    missing = [str(p.relative_to(WIKI)) for p in PAGES if p.name != "Home.md" and str(p.relative_to(WIKI)) not in home]
    assert not missing, f"Home.md не ссылается на: {missing}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(WIKI)))
def test_relative_links_resolve(page):
    text = page.read_text(encoding="utf-8")
    text = re.sub(r"```.*?```", "", text, flags=re.S)   # примеры кода — не ссылки
    text = re.sub(r"`[^`\n]*`", "", text)
    broken = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"^[a-z]+://|^#|^mailto:", target):
            continue
        path = target.split("#")[0]
        if path and not (page.parent / path).exists():
            broken.append(target)
    assert not broken, f"{page.relative_to(ROOT)}: битые ссылки {broken}"
