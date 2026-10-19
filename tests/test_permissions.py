"""Правила allow/ask/deny и режимы. См. docs/wiki/permissions.md."""

import pytest

from agent.permissions import Permissions, parse_rule, rule_matches
from agent.tools.base import Tool


class Shell(Tool):
    name = "shell"

    def subject(self, args):
        return args.get("command")


class Fetch(Tool):
    name = "web_fetch"

    def subject(self, args):
        return args.get("url")


class Reader(Tool):
    name, default_permission = "read_file", "allow"

    def subject(self, args):
        return args.get("path")


class Mcp(Tool):
    name = "mcp__github__create_issue"


def test_parse_rule():
    assert parse_rule("shell") == ("shell", None)
    assert parse_rule("shell(git *)") == ("shell", "git *")
    assert parse_rule(" web_fetch(domain:a.b) ") == ("web_fetch", "domain:a.b")


@pytest.mark.parametrize("rule,tool,args,ok", [
    ("shell", Shell(), {"command": "rm -rf /"}, True),
    ("shell(git *)", Shell(), {"command": "git status"}, True),
    ("shell(git *)", Shell(), {"command": "gitk"}, False),
    ("shell(*)", Shell(), {}, True),
    ("web_fetch(domain:example.com)", Fetch(), {"url": "https://docs.example.com/x"}, True),
    ("web_fetch(domain:example.com)", Fetch(), {"url": "https://example.com.evil.io/"}, False),
    ("web_fetch(https://a.io/*)", Fetch(), {"url": "https://a.io/page"}, True),
    ("mcp__github__*", Mcp(), {}, True),
    ("mcp__github__*(x)", Mcp(), {}, False),     # у MCP-инструментов нет предмета
    ("read_file(*.md)", Reader(), {"path": "docs/a.md"}, True),
])
def test_rule_matches(rule, tool, args, ok):
    assert rule_matches(rule, tool, args) is ok


def test_order_deny_beats_everything():
    p = Permissions({"deny": ["shell(rm *)"], "allow": ["shell"]})
    assert p.decide(Shell(), {"command": "rm x"})[0] == "deny"
    assert p.decide(Shell(), {"command": "rm x"}, hook_decision="allow")[0] == "deny"
    assert p.decide(Shell(), {"command": "ls"})[0] == "allow"


def test_hook_decisions():
    p = Permissions({"allow": ["shell"]})
    assert p.decide(Shell(), {"command": "ls"}, "deny")[0] == "deny"
    assert p.decide(Shell(), {"command": "ls"}, "ask")[0] == "ask"
    assert Permissions({}).decide(Shell(), {"command": "ls"}, "allow")[0] == "allow"


def test_defaults_and_modes():
    assert Permissions({}).decide(Shell(), {"command": "ls"})[0] == "ask"
    assert Permissions({}).decide(Reader(), {"path": "a"})[0] == "allow"
    assert Permissions({"mode": "allow_all"}).decide(Shell(), {"command": "ls"})[0] == "allow"
    # явное ask-правило сильнее режима allow_all
    assert Permissions({"mode": "allow_all", "ask": ["shell"]}).decide(Shell(), {"command": "ls"})[0] == "ask"
    ro = Permissions({"mode": "read_only"})
    assert ro.decide(Shell(), {"command": "ls"})[0] == "deny"
    assert ro.decide(Reader(), {"path": "a"})[0] == "allow"


def test_session_rules_and_ask_rules():
    p = Permissions({"ask": ["read_file(secret*)"]}, session_rules=["shell(make)"])
    assert p.decide(Shell(), {"command": "make"})[0] == "allow"
    assert p.decide(Shell(), {"command": "make install"})[0] == "ask"
    assert p.decide(Reader(), {"path": "secret.txt"})[0] == "ask"


def test_always_rule():
    assert Shell().always_rule({"command": "make test"}) == "shell(make test)"
    assert Mcp().always_rule({}) == "mcp__github__create_issue"
