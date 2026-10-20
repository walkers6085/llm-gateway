"""Права на вызов инструментов: правила allow/ask/deny и режимы.

Правило — `имя` или `имя(шаблон)`. Имя сравнивается через fnmatch (`mcp__github__*`),
шаблон — с «предметом» вызова (`Tool.subject`): командой для shell, путём для файлов,
URL для сети. `domain:example.com` в шаблоне сравнивается с хостом URL (включая поддомены).
Порядок решения описан в docs/wiki/permissions.md.
"""

from fnmatch import fnmatchcase
from urllib.parse import urlsplit

from .tools.base import Tool

ALLOW, ASK, DENY = "allow", "ask", "deny"
READ_ONLY_TOOLS = frozenset({"read_file", "list_dir", "glob", "grep", "web_fetch", "web_search", "goal", "todo", "question"})
MODES = ("default", "allow_all", "read_only")


def parse_rule(rule: str) -> tuple[str, str | None]:
    rule = rule.strip()
    if rule.endswith(")") and "(" in rule:
        name, _, pattern = rule[:-1].partition("(")
        return name.strip(), pattern
    return rule, None


def host_matches(host: str, domain: str) -> bool:
    host, domain = host.lower().rstrip("."), domain.lower().strip(".")
    return host == domain or host.endswith("." + domain)


def rule_matches(rule: str, tool: Tool, args: dict) -> bool:
    name, pattern = parse_rule(rule)
    if not fnmatchcase(tool.name, name):
        return False
    if pattern is None or pattern == "*":
        return True
    subject = tool.subject(args)
    if subject is None:
        return False
    if pattern.startswith("domain:"):
        host = urlsplit(subject).hostname or ""
        return host_matches(host, pattern[7:])
    return fnmatchcase(subject, pattern)


class Permissions:
    def __init__(self, config: dict, session_rules: list[str] | None = None):
        self.mode = config.get("mode", "default")
        self.allow = list(config.get("allow", [])) + list(session_rules or [])
        self.ask = list(config.get("ask", []))
        self.deny = list(config.get("deny", []))

    def _first(self, rules: list[str], tool: Tool, args: dict) -> str | None:
        return next((r for r in rules if rule_matches(r, tool, args)), None)

    def decide(self, tool: Tool, args: dict, hook_decision: str | None = None) -> tuple[str, str]:
        """→ (allow|ask|deny, причина). hook_decision — решение хука PreToolUse, если он его вернул."""
        if self.mode == "read_only" and tool.name not in READ_ONLY_TOOLS:
            return DENY, "инструмент недоступен в строгом режиме read_only"
        rule = self._first(self.deny, tool, args)
        if rule:
            return DENY, f"запрещено правилом {rule}"
        if hook_decision == DENY:
            return DENY, "запрещено хуком"
        if hook_decision == ALLOW:
            return ALLOW, "разрешено хуком"
        if hook_decision == ASK:
            return self._ask(tool, "хук требует подтверждения")
        rule = self._first(self.allow, tool, args)
        if rule:
            return ALLOW, f"разрешено правилом {rule}"
        rule = self._first(self.ask, tool, args)
        if rule:
            return self._ask(tool, f"правило {rule}")
        if self.mode == "read_only":
            return ALLOW, "инструмент чтения"
        if self.mode == "allow_all":
            return ALLOW, "режим allow_all"
        if tool.default_permission == ALLOW:
            return ALLOW, "разрешено по умолчанию"
        return self._ask(tool, "требует подтверждения")

    def _ask(self, tool: Tool, reason: str) -> tuple[str, str]:
        if self.mode == "read_only":
            return DENY, f"режим read_only ({reason})"
        return ASK, reason
