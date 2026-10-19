"""Именованные OpenAI-совместимые подключения для CLI и бенчмарков.

Управление: python -m agent.providers --help. Ключи хранятся только в окружении.
"""

import argparse
import asyncio
import copy
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .schema import validate

DEFAULT_FILE = "~/.config/gwagent/providers.json"
DEFAULT_CONFIG = {"default": "gateway", "providers": {
    "gateway": {"url": "http://127.0.0.1:7700/v1", "token_env": "GW_TOKEN", "model": "", "models": []}}}
STR = {"type": "string"}
PROVIDER_SCHEMA = {"type": "object", "required": ["url", "token_env", "model", "models"],
                   "properties": {"url": STR, "token_env": STR, "model": STR,
                                  "models": {"type": "array", "items": {"type": "string", "minLength": 1}}},
                   "additionalProperties": False}
SCHEMA = {"type": "object", "required": ["default", "providers"],
          "properties": {"default": STR, "providers": {"type": "object", "additionalProperties": PROVIDER_SCHEMA}},
          "additionalProperties": False}


@dataclass
class Connection:
    name: str
    url: str
    model: str
    token: str = field(repr=False)


class ProviderManager:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_FILE).expanduser()
        self.config = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else copy.deepcopy(DEFAULT_CONFIG)
        self.validate()

    def validate(self):
        validate(self.config, SCHEMA)
        providers = self.config["providers"]
        if self.config["default"] not in providers:
            raise ValueError("провайдер по умолчанию отсутствует в providers")
        for name, p in providers.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError(f"неверное имя провайдера: {name}")
            url = urlsplit(p["url"])
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError(f"{name}: нужен HTTP(S) URL без логина, пароля, query и fragment")
            if p["token_env"] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p["token_env"]):
                raise ValueError(f"{name}: неверное имя переменной token_env")

    def save(self):
        self.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".providers-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.config, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def get(self, name=None):
        name = name or self.config["default"]
        if name not in self.config["providers"]:
            raise ValueError(f"неизвестный провайдер: {name}")
        return self.config["providers"][name]

    def connection(self, name=None, model=None):
        name = name or self.config["default"]
        p = self.get(name)
        token = os.environ.get(p["token_env"], "") if p["token_env"] else ""
        if p["token_env"] and not token:
            raise ValueError(f"{name}: задайте переменную {p['token_env']}")
        return Connection(name, p["url"].rstrip("/"), model if model is not None else p["model"], token)

    def add(self, name, url, token_env="", model="", models=None):
        if name in self.config["providers"]:
            raise ValueError(f"провайдер {name} уже существует; измените JSON или сначала удалите его")
        self.config["providers"][name] = {"url": url, "token_env": token_env, "model": model, "models": models or []}
        self.save()

    def remove(self, name):
        self.get(name)
        if name == self.config["default"]:
            raise ValueError("сначала выберите другой провайдер по умолчанию через use")
        del self.config["providers"][name]
        self.save()

    def use(self, name, model=None):
        provider = self.get(name)
        self.config["default"] = name
        if model is not None:
            provider["model"] = model
        self.save()

    async def models(self, name=None, transport=None):
        connection = self.connection(name)
        headers = {"authorization": f"Bearer {connection.token}"} if connection.token else {}
        async with httpx.AsyncClient(base_url=connection.url + "/", headers=headers,
                                     trust_env=False, timeout=30, transport=transport) as client:
            response = await client.get("models")
            if response.status_code != 200:
                # Тело ответа может отражать секретный заголовок: его не печатаем.
                raise ValueError(f"{connection.name}: /models вернул HTTP {response.status_code}")
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise ValueError("/models: ожидается объект с массивом data")
            return [m["id"] for m in data["data"] if isinstance(m, dict) and isinstance(m.get("id"), str)]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Менеджер OpenAI-совместимых провайдеров")
    parser.add_argument("--file", default=DEFAULT_FILE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="показать провайдеров без ключей")
    add = sub.add_parser("add", help="добавить подключение")
    add.add_argument("name")
    add.add_argument("--url", required=True)
    add.add_argument("--token-env", default="", help="пусто — сервер без авторизации")
    add.add_argument("--model", default="")
    add.add_argument("--models", nargs="*", default=[])
    remove = sub.add_parser("remove")
    remove.add_argument("name")
    use = sub.add_parser("use")
    use.add_argument("name")
    use.add_argument("--model")
    models = sub.add_parser("models", help="получить список моделей через API")
    models.add_argument("name", nargs="?")
    args = parser.parse_args(argv)
    try:
        manager = ProviderManager(args.file)
        if args.command == "list":
            for name, p in manager.config["providers"].items():
                mark = "*" if name == manager.config["default"] else " "
                env = p["token_env"]
                auth = ("ключ задан" if os.environ.get(env) else f"нет {env}") if env else "без авторизации"
                print(f"{mark} {name}: {p['url']} · {p['model'] or '(модель не выбрана)'} · {auth}")
                if p["models"]:
                    print("  Модели: " + ", ".join(p["models"]))
        elif args.command == "add":
            manager.add(args.name, args.url, args.token_env, args.model, args.models)
        elif args.command == "remove":
            manager.remove(args.name)
        elif args.command == "use":
            manager.use(args.name, args.model)
        else:
            for model in asyncio.run(manager.models(args.name)):
                print(model)
        return 0
    except (ValueError, OSError, httpx.HTTPError) as exc:
        message = type(exc).__name__ if isinstance(exc, httpx.HTTPError) else str(exc)
        print(f"Провайдеры: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
