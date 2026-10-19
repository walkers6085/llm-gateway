"""gwagent — терминальный агент: модель через шлюз (токен gw-…), инструменты — на этой машине.

  gwagent                      интерактивный режим в текущей папке
  gwagent -p "задача"          один запрос и выход (для скриптов)
  gwagent --resume <id>        продолжить сессию; --continue — последнюю в этой папке

Справка — docs/wiki/cli.md.
"""

import argparse
import asyncio
import json
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

import httpx

from .llm import LLMClient, LLMError
from .mcp import MCPManager
from .runtime import make_agent, make_registry, start_mcp
from .settings import SettingsError, expand, load
from .store import Session, SessionStore

OWNER = "local"
HELP = """Команды:
  /new               новая сессия
  /sessions          последние сессии
  /resume <id>       продолжить сессию (достаточно начала id)
  /model [имя]       показать или сменить модель
  /tools             инструменты
  /mcp               MCP-серверы
  /exit              выход (или Ctrl+D)
Ctrl+C во время работы — отменить запрос."""


def short(value, limit=120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Console:
    """Печать событий агента. В режиме --json — по строке JSON на событие."""

    def __init__(self, out=None, err=None, as_json=False):
        self.out, self.err, self.as_json = out or sys.stdout, err or sys.stderr, as_json
        self.color = not as_json and getattr(self.out, "isatty", lambda: False)()
        self.mid_text = False

    def c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def line(self, text: str = "", err=False) -> None:
        self.end_text()
        print(text, file=self.err if err else self.out, flush=True)

    def end_text(self) -> None:
        if self.mid_text:
            print(file=self.out, flush=True)
            self.mid_text = False

    def event(self, ev: dict) -> None:
        if self.as_json:
            print(json.dumps(ev, ensure_ascii=False), file=self.out, flush=True)
            return
        kind = ev["type"]
        if kind == "text":
            self.out.write(ev["text"])
            self.out.flush()
            self.mid_text = True
        elif kind == "tool_call":
            self.line(self.c("36", f"⚙ {ev['name']}") + self.c("2", f" {short(ev['arguments'])}"))
        elif kind == "tool_result":
            mark = self.c("32", "✓") if ev["ok"] else self.c("31", "✗")
            first = short(ev["content"].splitlines()[0] if ev["content"] else "", 100)
            self.line(f"  {mark} {self.c('2', first)} {self.c('2', '(' + str(ev['ms']) + ' мс)')}")
        elif kind == "warning":
            self.line(self.c("33", f"⚠ {ev['message']}"), err=True)
        elif kind == "error":
            self.line(self.c("31", f"✗ {ev['message']}"), err=True)
        elif kind == "done":
            self.end_text()


class App:
    def __init__(self, args, settings: dict, workspace: Path, console: Console, read=input):
        self.args, self.settings, self.workspace, self.console = args, settings, workspace, console
        self.read = read
        self.interactive = args.prompt is None
        self.store = SessionStore(expand(settings["sessions"]["db"]))
        self.mcp: MCPManager | None = None
        self.http: httpx.AsyncClient | None = None
        self.llm: LLMClient | None = None
        self.session: Session | None = None
        self.agent = None
        self.task: asyncio.Task | None = None

    # ---------- подготовка ----------

    async def start(self) -> None:
        gw = self.settings["gateway"]
        token = gw.get("token") or os.environ.get(gw["token_env"], "")
        if not token:
            raise SettingsError(f"нет токена шлюза: задайте переменную {gw['token_env']} или gateway.token")
        self.http = httpx.AsyncClient(base_url=gw["url"].rstrip("/"), trust_env=False,
                                      timeout=httpx.Timeout(600, connect=15),
                                      headers={"authorization": f"Bearer {token}"})
        self.llm = LLMClient(self.http)
        if self.settings["mcp"]["servers"]:
            self.mcp = await start_mcp(self.settings)
            for name, error in self.mcp.errors.items():
                self.console.line(f"⚠ MCP {name}: {error}", err=True)
        self.session = await self.pick_session()
        self.rebuild()

    async def pick_session(self) -> Session:
        if self.args.resume:
            s = self.store.find(self.args.resume, owner=OWNER)
            if s is None:
                raise SettingsError(f"сессия {self.args.resume!r} не найдена (или префикс неоднозначен)")
            return s
        if self.args.continue_:
            for s in self.store.list(OWNER, limit=200):
                if s.workspace == str(self.workspace):
                    return s
        return self.store.create(OWNER, model=await self.default_model(), workspace=str(self.workspace))

    async def default_model(self) -> str:
        if self.settings["model"]:
            return self.settings["model"]
        try:
            models = await self.llm.models()
        except (LLMError, httpx.HTTPError) as exc:
            raise SettingsError(f"не удалось получить список моделей у шлюза: {exc}") from None
        if not models:
            raise SettingsError("шлюз не вернул ни одной модели; задайте --model")
        return models[0]

    def rebuild(self) -> None:
        self.agent = make_agent(llm=self.llm, settings=self.settings, store=self.store, session=self.session,
                                mcp=self.mcp, approver=self.approve if self.can_ask() else None)

    def can_ask(self) -> bool:
        return self.interactive or bool(getattr(sys.stdin, "isatty", lambda: False)())

    async def close(self) -> None:
        if self.mcp:
            await self.mcp.close()
        if self.http:
            await self.http.aclose()

    # ---------- подтверждения ----------

    async def approve(self, req: dict) -> str:
        con = self.console
        con.line(con.c("33", f"? {req['name']}: {short(req.get('subject') or req['arguments'], 300)}"))
        con.line(con.c("2", f"  ({req['reason']}; «всегда» добавит правило {req['rule']})"))
        loop = asyncio.get_running_loop()
        restore = self._remove_sigint(loop)
        try:
            while True:
                try:
                    answer = self.read("  Разрешить? [y]es / [n]o / [a]lways: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    if self.task:
                        self.task.cancel()
                    return "deny"
                if answer in ("y", "yes", "д", "да"):
                    return "allow"
                if answer in ("a", "always", "в", "всегда"):
                    return "always"
                if answer in ("n", "no", "н", "нет", ""):
                    return "deny"
        finally:
            restore()

    def _remove_sigint(self, loop):
        try:
            removed = loop.remove_signal_handler(signal.SIGINT)
        except (NotImplementedError, RuntimeError, ValueError):
            removed = False

        def restore():
            if removed and self.task and not self.task.done():
                loop.add_signal_handler(signal.SIGINT, self.task.cancel)
        return restore

    # ---------- работа ----------

    async def ask(self, text: str) -> str:
        """Один запрос к агенту; → причина done."""
        reason = "error"

        async def consume():
            nonlocal reason
            async for ev in self.agent.run(text):
                self.console.event(ev)
                if ev["type"] == "done":
                    reason = ev["reason"]

        loop = asyncio.get_running_loop()
        self.task = asyncio.create_task(consume())
        sigint = False
        try:
            loop.add_signal_handler(signal.SIGINT, self.task.cancel)
            sigint = True
        except (NotImplementedError, RuntimeError, ValueError):  # не главный поток (тесты) или не Unix
            pass
        try:
            await self.task
        except asyncio.CancelledError:
            self.console.line(self.console.c("33", "⏹ отменено"), err=True)
            reason = "cancelled"
        finally:
            if sigint:
                loop.remove_signal_handler(signal.SIGINT)
            self.task = None
        return reason

    async def repl(self) -> int:
        con = self.console
        con.line(con.c("1", "gwagent") + con.c("2", f" · {self.session.model} · {self.workspace} · "
                                                     f"сессия {self.session.id} · /help"))
        while True:
            try:
                text = self.read(con.c("1", "› ") if con.color else "› ").strip()
            except EOFError:
                con.line()
                return 0
            except KeyboardInterrupt:
                con.line()
                continue
            if not text:
                continue
            if text.startswith("/"):
                if await self.command(text) == "exit":
                    return 0
                continue
            await self.ask(text)

    async def command(self, text: str) -> str | None:
        con = self.console
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        if cmd in ("/exit", "/quit", "/q"):
            return "exit"
        if cmd == "/help":
            con.line(HELP)
        elif cmd == "/new":
            self.session = self.store.create(OWNER, model=self.session.model, workspace=str(self.workspace))
            self.rebuild()
            con.line(f"Новая сессия {self.session.id}")
        elif cmd == "/sessions":
            for s in self.store.list(OWNER, limit=20):
                mark = "*" if s.id == self.session.id else " "
                when = datetime.fromtimestamp(s.updated).strftime("%Y-%m-%d %H:%M")
                con.line(f"{mark} {s.id}  {when}  {short(s.title or '(без названия)', 50):<50}  {s.workspace}")
        elif cmd == "/resume":
            s = self.store.find(arg, owner=OWNER) if arg else None
            if s is None:
                con.line("Не найдена (или префикс неоднозначен): /resume <id>", err=True)
            else:
                self.session = s
                self.rebuild()
                con.line(f"Сессия {s.id}: {s.title} ({len(self.store.messages(s.id))} сообщений)")
        elif cmd == "/model":
            if arg:
                self.store.update(self.session.id, model=arg)
                self.session.model = arg
            con.line(f"Модель: {self.session.model}")
        elif cmd == "/tools":
            for t in make_registry(self.settings, self.mcp).tools.values():
                con.line(f"{t.name:<32} {t.default_permission:<6} {short(t.description, 80)}")
        elif cmd == "/mcp":
            status = self.mcp.status() if self.mcp else []
            if not status:
                con.line("MCP-серверы не настроены (mcp.servers)")
            for s in status:
                if s["ok"]:
                    con.line(f"✓ {s['name']}: {len(s['tools'])} инстр. — {', '.join(s['tools'])}")
                else:
                    con.line(f"✗ {s['name']}: {s['error']}")
        else:
            con.line(f"Неизвестная команда {cmd}; /help", err=True)
        return None


def parse_args(argv):
    p = argparse.ArgumentParser(prog="gwagent", description="Терминальный агент через LLM Gateway")
    p.add_argument("-p", "--prompt", help="выполнить один запрос и выйти")
    p.add_argument("--cwd", help="рабочая папка (по умолчанию текущая)")
    p.add_argument("--model", help="модель")
    p.add_argument("--settings", help="дополнительный файл настроек JSON")
    p.add_argument("--resume", metavar="ID", help="продолжить сессию")
    p.add_argument("-c", "--continue", dest="continue_", action="store_true",
                   help="продолжить последнюю сессию в этой папке")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--allow-all", action="store_true", help="не спрашивать подтверждений (permissions.mode=allow_all)")
    mode.add_argument("--read-only", action="store_true", help="запрещать всё, что требует подтверждения")
    p.add_argument("--json", action="store_true", help="события построчно в JSON (с -p)")
    return p.parse_args(argv)


async def amain(argv=None, read=input, out=None, err=None) -> int:
    args = parse_args(argv)
    console = Console(out, err, as_json=args.json)
    workspace = Path(args.cwd or os.getcwd()).expanduser().resolve()
    if not workspace.is_dir():
        console.line(f"gwagent: нет папки {workspace}", err=True)
        return 2
    overrides: dict = {}
    if args.model:
        overrides["model"] = args.model
    if args.allow_all or args.read_only:
        overrides["permissions"] = {"mode": "allow_all" if args.allow_all else "read_only"}
    app = None
    try:
        settings = load(workspace, extra=args.settings, overrides=overrides)
        app = App(args, settings, workspace, console, read)
        await app.start()
        if args.model and app.session.model != args.model:
            app.store.update(app.session.id, model=args.model)
            app.session.model = args.model
        if args.prompt is not None:
            reason = await app.ask(args.prompt)
            if not args.json:
                console.line(console.c("2", f"сессия {app.session.id}"), err=True)
            return 0 if reason == "stop" else 1
        return await app.repl()
    except SettingsError as exc:
        console.line(f"gwagent: {exc}", err=True)
        return 2
    finally:
        if app is not None:
            await app.close()


def main(argv=None) -> int:
    try:
        import readline  # noqa: F401 — история и редактирование строки в input()
    except ImportError:
        pass
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        return 130
