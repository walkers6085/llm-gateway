"""Настройки агента в JSON.

CLI собирает настройки из слоёв (каждый следующий главнее):
  DEFAULTS → ~/.config/gwagent/settings.json → <проект>/.gwagent/settings.json → --settings <файл>
Сервер читает config/agent.json: {"profiles": {"<имя>": {…те же ключи…}}}, каждый профиль
накладывается на DEFAULTS. Словари сливаются рекурсивно, списки и скаляры заменяются целиком.
Полная справка по ключам — docs/wiki/settings.md.
"""

import copy
import json
import os
from pathlib import Path

from .schema import SchemaError, validate
from .trust import TrustStore

USER_SETTINGS = Path("~/.config/gwagent/settings.json")
PROJECT_SETTINGS = Path(".gwagent/settings.json")

DEFAULT_SYSTEM_PROMPT = (
    "Ты — агент, который выполняет задачи пользователя с помощью инструментов. "
    "Работай в рабочей папке, перед правкой файла прочитай его, команды выполняй осмысленно. "
    "Если инструмент вернул ошибку — разберись в причине, а не повторяй вызов вслепую. "
    "Если доступны goal и todo, веди цель и список задач для многошаговой работы, обновляй их статусы. "
    "Если для работы не хватает важных данных и доступен question, задай уточняющий вопрос. "
    "Отвечай по-русски, кратко и по делу."
)

DEFAULTS: dict = {
    "model": "",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "temperature": None,
    "max_steps": 25,
    "max_input_tokens": 32768,
    "max_output_tokens": 1024,
    "max_turn_tokens": 65536,
    "model_profiles": {},
    "subagents": {"enabled": True, "max_calls": 3, "max_steps": 8, "timeout": 120},
    "model_options": {"tools": True, "stream": True, "temperature": True, "usage": True,
                      "max_tokens_field": "max_tokens"},
    "max_tool_output_chars": 30000,
    "tools": {
        "enabled": ["read_file", "write_file", "edit_file", "list_dir", "glob", "grep", "shell",
                    "web_fetch", "http_request", "web_search", "goal", "todo", "question"],
    },
    "permissions": {
        "mode": "default",
        "allow": [],
        "ask": [],
        "deny": [],
        "timeout": 600,
    },
    "files": {"max_read_bytes": 2_000_000, "timeout": 10, "max_scan_files": 1000},
    "shell": {
        "executable": "/bin/bash",
        "timeout": 120,
        "max_timeout": 600,
        "env_passthrough": ["PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "USER"],
        "env": {},
    },
    "network": {
        "timeout": 30,
        "max_bytes": 2_000_000,
        "max_redirects": 5,
        "proxy": "",
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) gwagent/0.1",
        "allow_private": False,
        "allow_hosts": [],
        "allow_domains": [],
        "deny_domains": [],
    },
    "search": {
        "provider": "duckduckgo",
        "duckduckgo_url": "https://html.duckduckgo.com/html/",
        "searxng_url": "",
        "max_results": 8,
    },
    "mcp": {"timeout": 60, "servers": {}},
    "hooks": {},
    "gateway": {"url": "http://127.0.0.1:7700/v1", "token_env": "GW_TOKEN"},
    "sessions": {"db": "~/.local/share/gwagent/sessions.db"},
    "workspace_root": "data/workspaces",
}

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]


def _obj(props: dict, extra=False) -> dict:
    return {"type": "object", "properties": props, "additionalProperties": extra}


STR, INT, BOOL = {"type": "string"}, {"type": "integer", "minimum": 0}, {"type": "boolean"}
POS = {"type": "integer", "minimum": 1}
STRS = {"type": "array", "items": STR}
STR_MAP = {"type": "object", "additionalProperties": STR}

MCP_SERVER = _obj({
    "type": {"type": "string", "enum": ["stdio", "http"]},
    "command": STR, "args": STRS, "env": STR_MAP, "cwd": STR,
    "url": STR, "headers": STR_MAP,
    "timeout": POS, "enabled": BOOL,
})
MCP_SERVER["required"] = ["type"]

HOOK = _obj({"matcher": STR, "command": {"type": "string", "minLength": 1}, "timeout": POS})
HOOK["required"] = ["command"]

MODEL_PROFILE = _obj({
    "max_input_tokens": POS, "max_output_tokens": POS, "max_turn_tokens": POS,
    "options": _obj({"tools": BOOL, "stream": BOOL, "usage": BOOL, "temperature": BOOL,
                     "max_tokens_field": {"enum": ["max_tokens", "max_completion_tokens"]}}),
})

SCHEMA = _obj({
    "model": STR,
    "system_prompt": STR,
    "temperature": {"type": ["number", "null"], "minimum": 0, "maximum": 2},
    "max_steps": POS,
    "max_input_tokens": POS, "max_output_tokens": POS, "max_turn_tokens": POS,
    "subagents": _obj({"enabled": BOOL, "max_calls": POS, "max_steps": POS, "timeout": POS}),
    "model_profiles": {"type": "object", "additionalProperties": MODEL_PROFILE},
    "model_options": _obj({"tools": BOOL, "stream": BOOL, "temperature": BOOL, "usage": BOOL,
                           "max_tokens_field": {"type": "string", "enum": ["max_tokens", "max_completion_tokens"]}}),
    "max_tool_output_chars": POS,
    "tools": _obj({"enabled": STRS}),
    "permissions": _obj({
        "mode": {"type": "string", "enum": ["default", "allow_all", "read_only"]},
        "allow": STRS, "ask": STRS, "deny": STRS, "timeout": POS,
    }),
    "files": _obj({"max_read_bytes": POS, "timeout": POS, "max_scan_files": POS}),
    "shell": _obj({"executable": STR, "timeout": POS, "max_timeout": POS,
                   "env_passthrough": STRS, "env": STR_MAP}),
    "network": _obj({"timeout": POS, "max_bytes": POS, "max_redirects": INT, "proxy": STR,
                     "user_agent": STR, "allow_private": BOOL, "allow_hosts": STRS,
                     "allow_domains": STRS, "deny_domains": STRS}),
    "search": _obj({"provider": {"type": "string", "enum": ["duckduckgo", "searxng"]},
                    "duckduckgo_url": STR, "searxng_url": STR, "max_results": POS}),
    "mcp": _obj({"timeout": POS, "servers": {"type": "object", "additionalProperties": MCP_SERVER}}),
    "hooks": _obj({event: {"type": "array", "items": HOOK} for event in HOOK_EVENTS}),
    "gateway": _obj({"url": STR, "token_env": STR, "token": STR}),
    "sessions": _obj({"db": STR}),
    "workspace_root": STR,
})


class SettingsError(ValueError):
    pass


def merge(base: dict, override: dict) -> dict:
    """Рекурсивное слияние: словари — по ключам, всё остальное заменяется."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def check(data: dict, source: str = "") -> dict:
    try:
        validate(data, SCHEMA)
    except SchemaError as exc:
        raise SettingsError(f"{source or 'настройки'}: {exc}") from None
    return data


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SettingsError(f"{path}: неверный JSON: {exc}") from None
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: ожидается JSON-объект")
    return data


def load(project_dir: str | Path | None = None, extra: str | Path | None = None,
         overrides: dict | None = None, user_file: Path | None = None) -> dict:
    """Настройки CLI: все слои, затем `overrides` (флаги командной строки)."""
    result = copy.deepcopy(DEFAULTS)
    layers = [(user_file or USER_SETTINGS).expanduser()]
    if project_dir is not None:
        project_file = Path(project_dir) / PROJECT_SETTINGS
        if project_file.is_file():
            content = project_file.read_bytes()
            if not TrustStore().trusted(project_dir, content):
                raise SettingsError("проектный конфиг не доверен или изменён; выполните gwagent trust в этой папке")
            # Применяем именно проверенные байты, повторного чтения после проверки нет.
            project_data = check(json.loads(content), str(project_file))
        else:
            project_data = None
    else:
        project_data = None
    if extra:
        path = Path(extra).expanduser()
        if not path.is_file():
            raise SettingsError(f"{path}: файл настроек не найден")
        layers.append(path)
    for index, path in enumerate(layers):
        if path.is_file():
            result = merge(result, check(read_json(path), str(path)))
        if index == 0 and project_data is not None:
            result = merge(result, project_data)
    if overrides:
        result = merge(result, check(overrides, "параметры командной строки"))
    return result


def load_profiles(path: str | Path) -> dict[str, dict]:
    """Профили сервера. Нет файла — один профиль `default` со значениями по умолчанию."""
    path = Path(path)
    if not path.is_file():
        return {"default": copy.deepcopy(DEFAULTS)}
    data = read_json(path)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise SettingsError(f"{path}: нужен непустой объект \"profiles\"")
    unknown = set(data) - {"profiles"}
    if unknown:
        raise SettingsError(f"{path}: неизвестные ключи верхнего уровня: {', '.join(sorted(unknown))}")
    return {name: merge(DEFAULTS, check(p, f"{path}: profiles.{name}"))
            for name, p in profiles.items()}


def expand(path: str, base: str | Path | None = None) -> Path:
    """`~` и переменные окружения; относительный путь — от `base`."""
    p = Path(os.path.expandvars(path)).expanduser()
    if not p.is_absolute() and base is not None:
        p = Path(base) / p
    return p


def flat_keys(data: dict = DEFAULTS, prefix: str = "") -> list[str]:
    """Пути всех ключей по умолчанию (`shell.timeout`, …) — для проверки, что вики их описывает."""
    keys = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        keys.append(path)
        if isinstance(value, dict) and value:
            keys.extend(flat_keys(value, path))
    return keys


def explain(project_dir=None, extra=None):
    from .credentials import redact
    result = load(project_dir, extra=extra)
    sources = {key: 'DEFAULTS' for key in flat_keys(result)}
    paths = [USER_SETTINGS.expanduser()]
    if project_dir is not None: paths.append(Path(project_dir) / PROJECT_SETTINGS)
    if extra: paths.append(Path(extra).expanduser())
    for path in paths:
        if path.is_file():
            for key in flat_keys(read_json(path)):
                sources[key] = str(path)
    safe = copy.deepcopy(result)
    safe['gateway'].pop('token', None)
    # Произвольные env/headers/команды также могут содержать введённые вручную секреты.
    for key in ('hooks', 'mcp'):
        safe[key] = '(скрыто; см. исходный файл)'
    safe['shell']['env'] = '(скрыто)'
    return {'values': json.loads(redact(json.dumps(safe))), 'sources': sources}
