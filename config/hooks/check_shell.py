#!/usr/bin/env python3
"""Хук PreToolUse для shell: запрещает явно опасные команды (код 2 = блокировка, причина в stderr).

Пример политики; подключается в config/agent.json → profiles.*.hooks.PreToolUse. См. docs/wiki/hooks.md.
"""

import json
import re
import sys

DANGEROUS = re.compile(r"\bsudo\b|\bmkfs\b|\bdd\s+if=|\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)(\s|$)|:\(\)\s*\{|"
                       r"\bshutdown\b|\breboot\b|\bcurl\b[^|]*\|\s*(ba)?sh\b")

data = json.load(sys.stdin)
command = data.get("tool_input", {}).get("command", "")
if DANGEROUS.search(command):
    print(f"Команда запрещена политикой сервера: {command}", file=sys.stderr)
    sys.exit(2)
