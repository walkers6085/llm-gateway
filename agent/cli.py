"""gwagent — терминальный агент: модель через шлюз (токен gw-…), инструменты — на этой машине.

  gwagent                      интерактивный режим в текущей папке
  gwagent -p "задача"          один запрос и выход (для скриптов)
  gwagent --resume <id>        продолжить сессию; --continue — последнюю в этой папке

Справка — docs/wiki/cli.md.
"""

import argparse
import asyncio
import copy
import json
import getpass
import os
import signal
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx

from .llm import LLMClient, LLMError
from .tracing import identifier
from .mcp import MCPManager, MCPError
from .providers import ProviderManager
from .credentials import redact
from .trust import TrustStore, fingerprint
from .runtime import make_agent, make_registry, start_mcp
from .settings import SettingsError, expand, load
from .store import Session, SessionStore, task_title

OWNER = "local"
COMMANDS = [
    {'command': '/provider import', 'description': 'Импортировать провайдеров из OpenCode', 'arguments': 'none'},
    {'command': '/provider export', 'description': 'Экспортировать провайдеров в OpenCode без секретов', 'arguments': 'none'},
    {'command': '/provider model', 'description': 'Настроить модель, контекст и параметры генерации', 'arguments': 'required', 'usage': '/provider model NAME [MODEL]'},
    {'command': '/data', 'description': 'Показать каталог настроек и данных', 'arguments': 'none'},
    {'command': '/mode', 'description': 'Выбрать агента build или plan', 'arguments': 'optional', 'usage': '/mode [build|plan]'},
    {'command': '/revert', 'description': 'Изменить прошлое сообщение или откатить файлы', 'arguments': 'none'},
    {'command': '/agent', 'description': 'Поручить задачу сабагенту', 'arguments': 'required', 'usage': '/agent задача'},
    {'command': '/logs', 'description': 'Журнал вызовов текущей сессии', 'arguments': 'optional', 'usage': '/logs [request_id]'},
    {'command': '/help', 'description': 'Справка по командам', 'arguments': 'none'},
    {'command': '/connect', 'description': 'Настроить провайдера и сохранить ключ', 'arguments': 'optional', 'usage': '/connect [имя]'},
    {'command': '/provider', 'description': 'Показать или выбрать провайдера', 'arguments': 'optional', 'usage': '/provider [имя]'},
    {'command': '/provider add', 'description': 'Добавить подключение', 'arguments': 'optional', 'usage': '/provider add [NAME]'},
    {'command': '/provider edit', 'description': 'Изменить URL и способ подключения', 'arguments': 'required', 'usage': '/provider edit NAME'},
    {'command': '/provider enable', 'description': 'Включить подключение', 'arguments': 'required', 'usage': '/provider enable NAME'},
    {'command': '/provider disable', 'description': 'Отключить подключение', 'arguments': 'required', 'usage': '/provider disable NAME'},
    {'command': '/provider remove', 'description': 'Удалить подключение и сохранённые ключи', 'arguments': 'required', 'usage': '/provider remove NAME'},
    {'command': '/provider set', 'description': 'Изменить URL и модель подключения', 'arguments': 'required', 'usage': '/provider set NAME URL [MODEL]'},
    {'command': '/model', 'description': 'Показать или выбрать модель', 'arguments': 'optional', 'usage': '/model [имя]'},
    {'command': '/models', 'description': 'Модели всех провайдеров и переключение', 'arguments': 'none'},
    {'command': '/tools', 'description': 'Доступные инструменты и параметры', 'arguments': 'optional', 'usage': '/tools [имя]'},
    {'command': '/mcp', 'description': 'Состояние MCP-серверов', 'arguments': 'none'},
    {'command': '/new', 'description': 'Создать новую сессию', 'arguments': 'none'},
    {'command': '/sessions', 'description': 'Список сохранённых сессий', 'arguments': 'none'},
    {'command': '/resume', 'description': 'Продолжить сохранённую сессию', 'arguments': 'optional', 'usage': '/resume [ID]'},
    {'command': '/compact', 'description': 'Сократить контекст с сохранением истории', 'arguments': 'none'},
    {'command': '/export', 'description': 'Экспортировать историю в файл', 'arguments': 'required', 'usage': '/export FILE'},
    {'command': '/delete', 'description': 'Удалить другую сессию с подтверждением', 'arguments': 'optional', 'usage': '/delete [ID]'},
    {'command': '/exit', 'description': 'Завершить работу', 'arguments': 'none'},
]
HELP = """Команды:
  /data              показать единый каталог данных
  /provider model NAME [MODEL]  параметры модели и лимиты
  /provider import|export  обмен настройками OpenCode
  /mode [build|plan]  показать или сменить активного агента
  /revert            выбрать прошлое сообщение и режим отката
  /agent задача      запустить отдельного сабагента
  /logs [request_id]  журнал запросов, моделей и инструментов
  /connect [имя]     настроить подключение и сохранить ключ/пароль
  /provider add [NAME]  пошагово добавить подключение
  /provider edit NAME   изменить URL, ключ или сертификат
  /provider enable|disable|remove NAME  управление подключениями
  /provider set NAME URL [MODEL]  изменить адрес и модель
  /compact           сократить контекст, сохранив историю
  /export FILE       экспорт текущей сессии
  /delete ID         удалить другую сессию
  /new               новая сессия
  /sessions          последние сессии
  /resume <id>       продолжить сессию (достаточно начала id)
  /model [имя]       показать или сменить модель
  /models            модели всех включённых провайдеров
  /provider [имя]    провайдеры; смена создаёт новую сессию
  /tools [имя]       инструменты или параметры одного инструмента
  /mcp               MCP-серверы
  /exit              выход (или Ctrl+D)
Ctrl+C во время работы — отменить запрос."""


def safe_terminal(text):
    text = re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))', '', text)
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32 and not 127 <= ord(c) < 160).replace('\x1b', '')


def short(value, limit=120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(safe_terminal(redact(text)).split())
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
        print(safe_terminal(redact(text)), file=self.err if err else self.out, flush=True)

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
            self.out.write(safe_terminal(redact(ev["text"])))
            self.out.flush()
            self.mid_text = True
        elif kind == "assistant" and ev.get('metrics'):
            metrics = ev['metrics']
            seconds = max(0, int(metrics.get('worked_ms', metrics['duration_ms']) / 1000))
            duration = (f"{seconds // 3600}h " if seconds >= 3600 else '') + (f"{seconds // 60 % 60}m " if seconds >= 60 else '') + f"{seconds % 60}s"
            finished = datetime.fromisoformat(metrics['completed_at']).astimezone().strftime('%H:%M')
            speed = metrics.get('tokens_per_second')
            self.line(self.c('2', f"Worked for {duration} • {finished} • " + (f"{speed:.1f} ток/с" if speed is not None else 'скорость н/д')))
        elif kind == "tool_call":
            self.line(self.c("36", f"⚙ {ev['name']}") + self.c("2", f" {short(ev['arguments'])}"))
        elif kind == "tool_result":
            mark = self.c("32", "✓") if ev["ok"] else self.c("31", "✗")
            first = short(ev["content"].splitlines()[0] if ev["content"] else "", 100)
            self.line(f"  {mark} {self.c('2', first)} {self.c('2', '(' + str(ev['ms']) + ' мс)')}")
        elif kind == "subagent_end":
            if ev.get("content"):
                self.line("↳ Сабагент:\n" + ev["content"])
        elif kind == "input_applied":
            self.line('↳ Уточнение учтено: ' + short(ev['text'], 160))
        elif kind == "input_rejected":
            self.line(ev['message'], err=True)
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
        self.mcp: MCPManager | None = None
        self.http: httpx.AsyncClient | None = None
        self.llm: LLMClient | None = None
        self.session: Session | None = None
        self.agent = None
        self.task: asyncio.Task | None = None
        self.active_request_id = None
        self.providers = ProviderManager(args.providers_file)
        self.store = SessionStore(expand(settings["sessions"]["db"]))
        self.provider = None
        self.base_profiles = copy.deepcopy(settings["model_profiles"])
        self.base_model = settings["model"]
        self.connection = None

    # ---------- подготовка ----------

    def provider_info(self, name):
        """Метаданные без чтения ключей, сертификатов и сетевых запросов."""
        from .providers import Connection
        if name is None:
            return Connection('gateway-settings', self.settings['gateway']['url'].rstrip('/'),
                              self.args.model or self.base_model, identity='gateway-settings')
        config = self.providers.get(name)
        return Connection(name, config['url'].rstrip('/'), self.args.model or config['model'],
                          identity=self.providers.identity(name))

    async def select_provider(self, name):
        connection = self.provider_info(name)
        if self.http:
            await self.http.aclose()
        self.http = self.llm = None
        self.provider, self.connection = name, connection
        self.settings['model'] = connection.model
        self.apply_model_profiles(name)

    def apply_model_profiles(self, name):
        from .settings import merge
        profiles = self.providers.get(name).get('model_profiles', {}) if name is not None else {}
        self.settings['model_profiles'] = merge(self.base_profiles, profiles)

    async def model_client(self):
        if self.llm is None:
            await self.connect_provider(self.provider)
        return self.llm

    async def switch_provider(self, name):
        await self.select_provider(name)
        self.session = self.create_session(self.settings['model'])
        self.rebuild()
        self.console.line(f"Провайдер: {name}; модель: {self.session.model or '(не выбрана)'}; новая сессия {self.session.id}")

    def provider_choices(self):
        choices = [{'provider': name, 'label': name + (' · ' + config['name'] if config.get('name') else ''), 'model': self.session.model if name == self.provider and self.session else config['model'],
                    'enabled': config.get('enabled', True)}
                   for name, config in self.providers.config['providers'].items()]
        gw = self.settings['gateway']
        if self.provider is None or gw.get('token') or os.environ.get(gw['token_env']):
            choices.insert(0, {'provider': None, 'label': 'gateway (настройки)',
                               'model': self.session.model if self.provider is None and self.session else self.base_model, 'enabled': True})
        return choices

    async def all_models(self):
        """Каталоги независимы: сбой API оставляет остальные модели и известные из конфига."""
        limit = asyncio.Semaphore(4)
        async def fetch(choice):
            name = choice['provider']
            config = self.providers.get(name) if name is not None else {'model': self.base_model}
            tags = config.get('tags', {}) if isinstance(config.get('tags', {}), dict) else {}
            models, error = [], None
            try:
                async with limit, asyncio.timeout(10):
                    connection = self.resolve_connection(name)
                    async with httpx.AsyncClient(base_url=connection.url.rstrip('/') + '/', trust_env=False,
                                                 timeout=10, **connection.http_kwargs()) as http:
                        models = await LLMClient(http).models()
                    if not models:
                        raise ValueError('API вернул пустой каталог')
            except (ValueError, OSError, SettingsError, LLMError, httpx.HTTPError, httpx.InvalidURL, TimeoutError) as exc:
                error = redact(str(exc)) or type(exc).__name__
                models = [*config.get('models', []), config['model']]
            models = [*models, *config.get('models', []), *config.get('model_profiles', {}), *config.get('model_names', {}), *(m for m in tags if m)]
            entries = [{'provider': name, 'provider_name': choice['label'], 'model': model,
                        'display_name': config.get('model_names', {}).get(model, model),
                        'available': error is None, 'tags': sorted(tags.get(model, []))}
                       for model in dict.fromkeys(models) if model]
            return entries, ({'provider': choice['label'], 'message': error} if error else None)
        results = await asyncio.gather(*(fetch(c) for c in self.provider_choices() if c['enabled']))
        return [m for entries, _ in results for m in entries], [error for _, error in results if error]

    async def choose_model(self, provider, model):
        if provider != self.provider:
            if provider is not None and not self.providers.get(provider).get('enabled', True):
                raise ValueError('подключение отключено')
            await self.select_provider(provider)
            self.session = self.create_session(model)
        else:
            self.store.update(self.session.id, model=model)
            self.session.model = model
        self.rebuild()
        self.console.line(f"Модель: {model}; провайдер: {provider or 'gateway (настройки)'}")

    def configure_provider(self, name, read=input, password=getpass.getpass):
        return self.providers.configure(name, read=read, password=password,
            default_url=self.settings['gateway']['url'] if name == 'gateway' and not self.providers.path.exists() else None,
            default_model=self.base_model if name == 'gateway' else '')

    def import_providers(self, read=input):
        from .opencode import prepare
        path = read('Путь к opencode.json или opencode.jsonc: ').strip()
        replace = read('Объединить совпадающие провайдеры? [y/N]: ').strip().lower() in ('y', 'да')
        candidate, secrets, names = prepare(self.providers, path, replace=replace)
        if read('Импортировать проверенные подключения ' + ', '.join(names) + '? [y/N]: ').strip().lower() not in ('y', 'да'):
            return False
        self.providers.save_connections(candidate, secrets)
        return True

    def export_providers(self, read=input):
        from .opencode import export_config
        name = read('Провайдер для экспорта (Enter — все): ').strip()
        path = read('Путь нового файла экспорта OpenCode: ').strip()
        variables = export_config(self.providers, path, providers=[name] if name else None)
        self.console.line('Экспорт создан без секретов' + ('; задайте переменные: ' + ', '.join(variables) if variables else ''))

    def manage_provider(self, action, name, read=input):
        if action != 'enable' and (name == self.provider or name == self.providers.config['default']):
            raise ValueError('Сначала выберите другой провайдер; текущее подключение нельзя отключить или удалить')
        self.providers.get(name)
        if action == 'remove':
            if read(f'Удалить подключение {name} и его сохранённые ключи? [y/N]: ').strip().lower() not in ('y', 'да'):
                return
            self.providers.remove(name)
        elif action in ('enable', 'disable'):
            self.providers.update(name, enabled=action == 'enable')
        else:
            raise ValueError('Доступны enable, disable, remove')
        self.console.line(f'Подключение {name}: ' + {'remove': 'удалено', 'enable': 'включено', 'disable': 'отключено'}[action])

    async def start(self) -> None:
        if self.settings["mcp"]["servers"]:
            self.mcp = await start_mcp(self.settings)
            for name, error in self.mcp.errors.items():
                self.console.line(f"⚠ MCP {name}: {error}", err=True)
        self.session = await self.pick_session()
        self.rebuild()

    def resolve_connection(self, name):
        if name is not None:
            return self.providers.connection(name, self.args.model)
        else:
            gw = self.settings["gateway"]
            token = gw.get("token") or os.environ.get(gw["token_env"], "")
            if not token:
                raise SettingsError(f"нет токена шлюза: задайте переменную {gw['token_env']} или gateway.token")
            url, model = gw["url"], self.base_model
            from .providers import Connection
            from .credentials import register
            register(token)
            connection = Connection('gateway-settings', url.rstrip('/'), model, token, 'gateway-settings')
            return connection

    async def connect_provider(self, name, close_previous=True):
        connection = self.resolve_connection(name)
        url, model = connection.url, connection.model
        kwargs = connection.http_kwargs()
        http = httpx.AsyncClient(base_url=url.rstrip("/"), trust_env=False,
                                timeout=httpx.Timeout(600, connect=15),
                                **kwargs)
        if self.http and close_previous:
            await self.http.aclose()
        self.http, self.llm = http, LLMClient(http, options={**self.settings["model_options"], **connection.options})
        self.connection = connection
        self.provider = name
        self.settings["model"] = model
        self.apply_model_profiles(name)

    async def activate_session(self, session, enforce_explicit=False):
        if Path(session.workspace).resolve() != self.workspace:
            raise SettingsError(f"сессия из другой папки; запустите с --cwd {session.workspace}")
        name = session.profile.removeprefix("provider:") if session.profile.startswith("provider:") else None
        if enforce_explicit and self.args.provider and name != self.args.provider:
            raise SettingsError("сессия принадлежит другому провайдеру; возобновите без --provider или создайте новую")
        candidate = self.provider_info(name)
        if candidate and session.connection_id and (session.connection_id != candidate.identity or session.endpoint_hash != candidate.endpoint_hash):
            raise SettingsError('подключение сессии изменилось; создайте новую сессию или восстановите исходный провайдер')
        if session.endpoint_hash and session.endpoint_hash != candidate.endpoint_hash:
            raise SettingsError('адрес подключения сессии изменился; создайте новую сессию')
        if self.interactive:
            await self.select_provider(name)
        elif name != self.provider or self.http is None:
            await self.connect_provider(name)
        if session.endpoint_hash and session.endpoint_hash != self.connection.endpoint_hash:
            raise SettingsError('адрес подключения сессии изменился; создайте новую сессию')
        return session

    def create_session(self, model):
        return self.store.create(OWNER, model=model, workspace=str(self.workspace),
                                 profile=f"provider:{self.provider}" if self.provider else "",
                                 connection_id=self.connection.identity, endpoint_hash=self.connection.endpoint_hash)

    async def pick_session(self) -> Session:
        if self.args.resume:
            s = self.store.find(self.args.resume, owner=OWNER)
            if s is None:
                raise SettingsError(f"сессия {self.args.resume!r} не найдена (или префикс неоднозначен)")
            return await self.activate_session(s, enforce_explicit=True)
        if self.args.continue_:
            for s in self.store.list(OWNER, limit=200):
                if s.workspace == str(self.workspace):
                    return await self.activate_session(s, enforce_explicit=True)
        name = self.args.provider or (self.providers.config["default"] if self.args.providers_file or self.providers.selected else None)
        if self.interactive:
            await self.select_provider(name)
            return self.create_session(self.settings['model'])
        await self.connect_provider(name)
        return self.create_session(await self.default_model())

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
        if self.llm is None:
            self.agent = None
            return
        self.agent = make_agent(llm=self.llm, settings=self.settings, store=self.store, session=self.session,
                                mcp=self.mcp, approver=self.approve if self.can_ask() else None,
                                questioner=self.question if self.can_ask() else None)

    def can_ask(self) -> bool:
        return self.interactive or bool(getattr(sys.stdin, "isatty", lambda: False)())

    def command_catalog(self):
        """Меню доступно без LLM; инструменты открывают справку, а не выполняются."""
        commands = [{'kind': 'command', **item} for item in COMMANDS]
        tools = [{'kind': 'tool', 'command': '/tools ' + tool.name, 'name': tool.name,
                  'description': tool.description, 'permission': tool.default_permission,
                  'arguments': 'none'}
                 for tool in make_registry(self.settings, self.mcp, agent_mode=self.session.agent_mode).tools.values()]
        return commands + tools

    async def close(self) -> None:
        if self.mcp:
            await self.mcp.close()
        if self.http:
            await self.http.aclose()
        self.store.close()

    def set_agent_mode(self, mode):
        if mode not in ('build', 'plan'):
            raise ValueError('агент: build | plan')
        if self.task and not self.task.done():
            raise ValueError('дождитесь завершения запроса перед сменой агента')
        self.store.update(self.session.id, agent_mode=mode)
        self.session.agent_mode = mode
        self.rebuild()

    async def question(self, request):
        self.console.line('Вопрос агента: ' + request['question'])
        options = request.get('options', [])
        for index, text in enumerate(options, 1):
            self.console.line(f'  {index}. {text}')
        restore = self._remove_sigint(asyncio.get_running_loop())
        try:
            answer = self.read('Ответ (номер или свой текст; Enter — пропустить): ').strip()
        except (EOFError, KeyboardInterrupt):
            return None
        finally:
            restore()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            answer = options[int(answer)-1]
        return answer or None

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

    async def ask(self, text: str, *, delegate=False) -> str:
        """Один запрос к агенту; → причина done."""
        reason = "error"
        request_id = identifier()
        self.active_request_id = request_id
        if not self.session.title or self.session.title_auto and self.session.title.startswith('Новая сессия ·'):
            self.session.title = task_title(text)
            self.store.update(self.session.id, title=self.session.title)

        async def consume():
            nonlocal reason
            try:
                await self.model_client()
                if not self.session.model:
                    self.session.model = await self.default_model()
                    self.store.update(self.session.id, model=self.session.model)
                self.rebuild()
            except (SettingsError, ValueError, OSError, LLMError, httpx.HTTPError) as exc:
                self.store.trace(request_id, request_id, '', self.session.id, 'request.failed', {'state': 'initialization_error'})
                self.console.event({'type': 'error', 'request_id': request_id, 'message': f'{exc}. Настройте /connect [имя], /provider и /model; команды остаются доступны.'})
                self.console.event({'type': 'done', 'reason': 'error', 'request_id': request_id})
                return
            async for ev in self.agent.run(text, request_id=request_id, delegate=delegate):
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
            self.console.event({"type": "done", "reason": "cancelled", "request_id": request_id})
        finally:
            if sigint:
                loop.remove_signal_handler(signal.SIGINT)
            self.task = None
            self.active_request_id = None
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

    def revert_messages(self):
        return [row for row in self.store.timeline(self.session.id) if row['message']['role'] == 'user'
                and ('user_input' in row['metadata'] or not row['metadata'].get('request_id'))]

    def revert_preview(self, seq):
        from .checkpoints import plan
        checkpoints = self.store.revert_checkpoints(self.session.id, seq)
        original, desired = plan(self.workspace, checkpoints)
        return sum(original[name] != value for name, value in desired.items())

    def apply_revert(self, seq, revision, *, files=False):
        result = self.store.rewind(self.session.id, seq, revision=revision, files=files)
        self.session = self.store.get(self.session.id)
        self.rebuild()
        return result

    async def command(self, text: str) -> str | None:
        con = self.console
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        if cmd in ("/exit", "/quit", "/q"):
            return "exit"
        if cmd == '/data':
            from .data import home, paths
            con.line(json.dumps({'home': str(home()), 'paths': paths()}, ensure_ascii=False, indent=2))
        elif cmd == '/connect':
            try:
                name = arg or self.provider or self.providers.config['default']
                self.configure_provider(name, read=self.read)
                await self.switch_provider(name)
                con.line('Учётные данные сохранены; подключение выбрано')
            except (EOFError, KeyboardInterrupt):
                con.line('Настройка подключения отменена', err=True)
            except (ValueError, OSError) as exc:
                con.line(redact(str(exc)), err=True)
        elif cmd == '/revert':
            messages = self.revert_messages()
            if not messages:
                con.line('Нет пользовательских сообщений для изменения')
                return
            revision = self.store.revision(self.session.id)
            for index, row in enumerate(messages, 1):
                con.line(f"{index}. {short(row['metadata'].get('user_input', row['message'].get('content') or ''))}")
            answer = self.read('Номер сообщения (Enter — отмена): ').strip()
            if not answer:
                return
            if not answer.isdigit() or not 1 <= int(answer) <= len(messages):
                con.line('Неверный номер сообщения', err=True)
                return
            seq = messages[int(answer)-1]['seq']
            try:
                count = self.revert_preview(seq)
                con.line(f'1 — исправить текст; 2 — исправить текст и отменить все изменения ({count} файлов/папок); Enter — отмена')
            except ValueError as exc:
                con.line(str(exc))
                con.line('1 — исправить текст; Enter — отмена')
            mode = self.read('Режим: ').strip()
            if mode not in ('1', '2'):
                return
            edited = self.read('Новый текст сообщения (Enter — отмена): ')
            if not edited.strip():
                return
            try:
                self.apply_revert(seq, revision, files=mode == '2')
            except (OSError, ValueError) as exc:
                con.line(str(exc), err=True)
                return
            await self.ask(edited)
        elif cmd == '/agent':
            if arg:
                await self.ask(arg, delegate=True)
            else:
                con.line('Использование: /agent задача', err=True)
        elif cmd == '/logs':
            for row in self.store.traces(self.session.id, arg or None):
                con.line(json.dumps(row, ensure_ascii=False))
        elif cmd == '/mode':
            try:
                if arg:
                    self.set_agent_mode(arg.strip())
                con.line('Активный агент: ' + self.session.agent_mode)
            except ValueError as exc:
                con.line(str(exc), err=True)
        elif cmd == '/compact':
            changed = self.store.compact(self.session.id)
            self.session = self.store.get(self.session.id)
            self.rebuild()
            con.line('Контекст сокращён; исходная история сохранена' if changed else 'Контекст уже минимален: последний запрос сохранён полностью')
        elif cmd == '/export':
            if not arg:
                con.line('Нужен путь: /export FILE', err=True)
            else:
                from .storage import write_json
                write_json(Path(arg).expanduser(), self.store.export(self.session.id))
                con.line('История экспортирована')
        elif cmd == '/delete':
            target = self.store.find(arg, owner=OWNER) if arg else None
            if target is None or target.id == self.session.id:
                con.line('Укажите ID другой сессии; для текущей сначала /new', err=True)
            elif self.read('Удалить историю? [y/N]: ').lower() in ('y', 'да'):
                self.store.delete(target.id)
                con.line('Сессия удалена')
        elif cmd == "/help":
            con.line(HELP)
        elif cmd == "/new":
            self.session = self.create_session(self.session.model)
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
                try:
                    self.session = await self.activate_session(s)
                except (ValueError, SettingsError) as exc:
                    con.line(str(exc), err=True)
                    return None
                self.rebuild()
                con.line(f"Сессия {s.id}: {s.title} ({len(self.store.messages(s.id))} сообщений)")
        elif cmd == "/provider":
            if not arg:
                con.line(f"Текущий провайдер: {self.provider or '(gateway из настроек)'}")
                for name, provider in self.providers.config["providers"].items():
                    con.line(f"  {name}: {provider['url']} · {provider['model'] or '(без модели)'}")
            elif arg in ('import', 'export'):
                try:
                    if arg == 'import':
                        if self.import_providers(read=self.read):
                            await self.switch_provider(self.providers.config['default'])
                            con.line('Провайдеры импортированы')
                    else:
                        self.export_providers(read=self.read)
                except (EOFError, KeyboardInterrupt):
                    con.line('Операция отменена', err=True)
                except (ValueError, OSError) as exc:
                    con.line(redact(str(exc)), err=True)
            elif arg.startswith('model '):
                fields = arg.split(maxsplit=2)
                try:
                    self.providers.configure_model(fields[1], fields[2] if len(fields) == 3 else None, read=self.read)
                    if self.provider == fields[1]:
                        self.apply_model_profiles(self.provider);self.rebuild()
                    con.line('Параметры модели сохранены')
                except (ValueError, OSError) as exc:
                    con.line(redact(str(exc)), err=True)
            elif arg == 'add' or (arg.startswith(('add ', 'edit ')) and len(arg.split()) == 2):
                fields = arg.split()
                name = fields[1] if len(fields) == 2 else None
                try:
                    if fields[0] == 'edit':
                        self.providers.get(name)
                    elif name and name in self.providers.config['providers']:
                        raise ValueError('Провайдер уже существует; используйте /provider edit ' + name)
                    name = self.configure_provider(name, read=self.read)
                    await self.switch_provider(name)
                    con.line('Подключение сохранено и выбрано')
                except (ValueError, OSError) as exc:
                    con.line(redact(str(exc)), err=True)
            elif arg.startswith(('enable ', 'disable ', 'remove ')):
                try:
                    action, name = arg.split()
                    self.manage_provider(action, name, read=self.read)
                except (EOFError, KeyboardInterrupt):
                    con.line('Изменение подключения отменено', err=True)
                except (ValueError, OSError) as exc:
                    con.line(redact(str(exc)), err=True)
            elif arg.startswith(('add ', 'set ')):
                fields = arg.split()
                try:
                    if len(fields) not in (3, 4):
                        raise ValueError('используйте /provider add|set NAME URL [MODEL]')
                    if fields[0] == 'add':
                        self.providers.add(fields[1], fields[2], model=fields[3] if len(fields) == 4 else '')
                    else:
                        changes = {'url': fields[2]}
                        if len(fields) == 4:
                            changes['model'] = fields[3]
                        self.providers.update(fields[1], **changes)
                        if self.provider == fields[1]:
                            await self.switch_provider(fields[1])
                    con.line(f'Подключение {fields[1]} сохранено; /connect {fields[1]} — ключ, /provider {fields[1]} — выбрать')
                except (ValueError, OSError) as exc:
                    con.line(str(exc), err=True)
            else:
                try:
                    await self.switch_provider(arg)
                except (ValueError, SettingsError, OSError) as exc:
                    con.line(f"Провайдер: {exc}", err=True)
        elif cmd == "/models":
            try:
                models, errors = await self.all_models()
                for entry in models:
                    tail = (' · ' + ', '.join(entry['tags'])) if entry.get('tags') else ''
                    tail += '' if entry['available'] else ' · из конфига, API недоступен'
                    con.line(f"{entry['provider_name']} / {entry['model']}{tail}")
                for error in errors:
                    con.line(f"Модели {error['provider']}: {error['message']}", err=True)
            except (SettingsError, ValueError, OSError, LLMError, httpx.HTTPError) as exc:
                con.line(f"Модели: {exc}", err=True)
        elif cmd == "/model":
            if arg:
                self.store.update(self.session.id, model=arg)
                self.session.model = arg
            con.line(f"Модель: {self.session.model}")
        elif cmd == "/tools":
            registry = make_registry(self.settings, self.mcp, agent_mode=self.session.agent_mode)
            if arg:
                tool = registry.get(arg)
                if tool is None:
                    con.line(f'Неизвестный инструмент: {arg}', err=True)
                else:
                    con.line(f'{tool.name} · разрешение по умолчанию: {tool.default_permission}')
                    con.line(tool.description)
                    con.line(json.dumps(tool.parameters, ensure_ascii=False, indent=2))
            else:
                for t in registry.tools.values():
                    con.line(f"{t.name:<32} {t.default_permission:<6} {short(t.description, 80)}")
        elif cmd == "/mcp":
            status = self.mcp.status(include_disabled=True) if self.mcp else []
            if not status:
                con.line("MCP-серверы не настроены (mcp.servers)")
            for s in status:
                if s.get('state') == 'disabled':
                    con.line(f"– {s['name']}: выключен в настройках")
                elif s["ok"]:
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
    p.add_argument("--provider", help="имя подключения из менеджера провайдеров")
    p.add_argument("--providers-file", help="JSON провайдеров; без --provider используется default")
    p.add_argument("--settings", help="дополнительный файл настроек JSON")
    p.add_argument("--resume", metavar="ID", help="продолжить сессию")
    p.add_argument("-c", "--continue", dest="continue_", action="store_true",
                   help="продолжить последнюю сессию в этой папке")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--allow-all", action="store_true", help="не спрашивать подтверждений (permissions.mode=allow_all)")
    mode.add_argument("--read-only", action="store_true", help="запрещать всё, что требует подтверждения")
    p.add_argument("--json", action="store_true", help="события построчно в JSON (с -p)")
    display = p.add_mutually_exclusive_group()
    display.add_argument("--tui", action="store_true", help="интерфейс OpenTUI (Bun)")
    display.add_argument("--plain", action="store_true", help="обычный терминальный интерфейс")
    return p.parse_args(argv)


async def amain(argv=None, read=input, out=None, err=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in ('trust', 'doctor', 'config', 'connect', 'data'):
        return await utility(argv, read, out, err)
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
    except (SettingsError, ValueError, OSError, MCPError) as exc:
        console.line(f"gwagent: {exc}", err=True)
        return 2
    finally:
        if app is not None:
            await app.close()


def main(argv=None) -> int:
    from .tui import dispatch
    result = dispatch(list(sys.argv[1:] if argv is None else argv))
    if result is not None:
        return result
    try:
        import readline  # noqa: F401 — история и редактирование строки в input()
    except ImportError:
        pass
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        return 130


async def utility(argv, read=input, out=None, err=None):
    parser = argparse.ArgumentParser(prog='gwagent ' + argv[0])
    parser.add_argument('--cwd', default=os.getcwd())
    parser.add_argument('--revoke', action='store_true')
    parser.add_argument('--settings')
    parser.add_argument('--providers-file')
    parser.add_argument('--provider')
    parser.add_argument('action', nargs='?', default=None if argv[0] == 'connect' else 'show')
    args = parser.parse_args(argv[1:])
    workspace = Path(args.cwd).expanduser().resolve()
    console = Console(out, err)
    try:
        if argv[0] == 'data':
            from .data import home, paths, migrate
            if args.action == 'migrate':
                copied = migrate()
                console.line('Перенесено: ' + (', '.join(copied) or 'старые файлы не найдены'))
            elif args.action != 'show':
                raise ValueError('использование: gwagent data [show|migrate]')
            console.line(json.dumps({'home': str(home()), 'paths': paths()}, ensure_ascii=False, indent=2))
            return 0
        if argv[0] == 'connect':
            settings = load(workspace, extra=args.settings)
            manager = ProviderManager(args.providers_file)
            name = args.provider or args.action or manager.config['default']
            manager.configure(name, read=read,
                default_url=settings['gateway']['url'] if name == 'gateway' and not manager.path.exists() else None,
                default_model=settings['model'] if name == 'gateway' else '')
            console.line('Учётные данные сохранены; запуск чата: gwagent')
            return 0
        if argv[0] == 'trust':
            stamp = None
            if not args.revoke:
                config = workspace / '.gwagent/settings.json'
                stamp = fingerprint(config)
                console.line(f'Доверие проектному конфигу {config}; SHA-256 {stamp}; он может запускать команды и менять подключения')
                if read('Доверять текущему содержимому? [y/N]: ').strip().lower() not in ('y', 'да'):
                    return 1
            TrustStore().set(workspace, not args.revoke, expected=stamp)
            console.line('Доверие отозвано' if args.revoke else 'Доверие сохранено')
            return 0
        settings = load(workspace, extra=args.settings)
        if argv[0] == 'config':
            if args.action != 'show':
                raise ValueError('доступна команда config show')
            from .settings import explain
            details = explain(workspace, args.settings)
            manager = ProviderManager(args.providers_file)
            if args.provider or args.providers_file or manager.selected:
                name = args.provider or manager.config['default']
                provider = manager.get(name)
                details['connection'] = {'provider': name, 'url': provider['url'], 'model': provider['model'],
                    'enabled': provider.get('enabled', True),
                    'auth': provider.get('auth', 'env' if provider['token_env'] else 'none'),
                    'tls': provider.get('tls', {}), 'source': str(manager.path)}
                details['values']['model'] = provider['model']
                details['sources']['model'] = str(manager.path)
            else:
                details['connection'] = {'provider': '(gateway из настроек)', 'url': settings['gateway']['url'], 'model': settings['model']}
            console.line(json.dumps(details, ensure_ascii=False, indent=2))
            return 0
        from .doctor import diagnose
        checks = await diagnose(settings, args.provider, args.providers_file)
        for check in checks:
            console.line(('✓ ' if check['ok'] else '✗ ') + check['message'])
        return 0 if all(c['ok'] for c in checks) else 1
    except (EOFError, KeyboardInterrupt):
        console.line('Настройка отменена: ввод прерван', err=True)
        return 1
    except (ValueError, OSError) as exc:
        console.line(redact(str(exc)), err=True)
        return 2
