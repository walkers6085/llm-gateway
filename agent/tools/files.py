"""Файловые инструменты в пределах рабочей папки: read_file, write_file, edit_file, list_dir, glob, grep.

Любой путь (относительный — от рабочей папки) проходит через `resolve()`, включая симлинки,
и должен остаться внутри рабочей папки. Подробности — docs/wiki/tools/files.md.
"""

import os
import asyncio
import difflib
import stat
import multiprocessing
import tempfile
import hashlib
from itertools import islice
import re
from fnmatch import fnmatch
from pathlib import Path

from .base import Tool, ToolContext, ToolError, normalize_rel
from ..credentials import protected, _PROTECTED, protect
from ..storage import atomic_write, digest, file_lock

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache"}
MAX_LIST = 500
MAX_MATCHES = 200


def resolve(ctx: ToolContext, path: str) -> Path:
    root = ctx.workspace.resolve()
    target = (root / path).resolve() if path else root
    if target != root and root not in target.parents:
        raise ToolError(f"путь {path!r} вне рабочей папки")
    if protected(target):
        raise ToolError("доступ к файлу учётных данных/сертификату запрещён")
    return target


def rel(ctx: ToolContext, path: Path) -> str:
    return str(path.relative_to(ctx.workspace.resolve())) or "."


def read_text(path: Path, limit: int) -> str:
    if not path.is_file():
        raise ToolError(f"{path.name}: не файл или не существует")
    if path.stat().st_size > limit:
        raise ToolError(f"{path.name}: файл больше {limit} байт (files.max_read_bytes)")
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        raise ToolError(f"{path.name}: двоичный файл")
    return data.decode("utf-8", "replace")


class PathTool(Tool):
    async def run(self, args, ctx):
        if self.name not in ('grep', 'glob', 'list_dir'):
            return await asyncio.to_thread(self._run, args, ctx)
        # Регулярное выражение выполняется в отдельном процессе: его можно остановить.
        mp = multiprocessing.get_context('spawn')
        parent, child = mp.Pipe(duplex=False)
        process = mp.Process(target=_file_worker, args=(child, self.name, args, str(ctx.workspace), ctx.settings['files'], [str(p) for p in _PROTECTED]))
        process.start()
        child.close()
        try:
            deadline = asyncio.get_running_loop().time() + ctx.settings['files'].get('timeout', 10)
            while not parent.poll():
                if not process.is_alive():
                    raise ToolError('процесс поиска завершился без ответа')
                if asyncio.get_running_loop().time() >= deadline:
                    raise ToolError('таймаут поиска; уточните выражение или папку')
                await asyncio.sleep(.02)
            ok, result = parent.recv()
            if not ok:
                raise ToolError(result)
            return result
        finally:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 1)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 1)
            parent.close()
            process.close()

    def subject(self, args: dict) -> str | None:
        return normalize_rel(args.get("path", ""))


class ReadFile(PathTool):
    plan_safe = True
    name = "read_file"
    default_permission = "allow"
    description = ("Прочитать текстовый файл. Возвращает строки с номерами (`номер<TAB>текст`). "
                   "Для больших файлов используй offset/limit.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "путь относительно рабочей папки"},
        "offset": {"type": "integer", "minimum": 1, "description": "с какой строки (с 1)"},
        "limit": {"type": "integer", "minimum": 1, "description": "сколько строк (по умолчанию 2000)"},
    }, "required": ["path"], "additionalProperties": False}

    def _run(self, args, ctx):
        path = resolve(ctx, args["path"])
        before = digest(path) if path.is_file() else None
        lines = read_text(path, ctx.settings["files"]["max_read_bytes"]).splitlines()
        if digest(path) != before:
            raise ToolError('файл изменился во время чтения')
        ctx.extra.setdefault('read_hashes', {})[str(path)] = before
        start = args.get("offset", 1)
        chunk = lines[start - 1:start - 1 + args.get("limit", 2000)]
        if not chunk:
            return f"(пусто: в файле {len(lines)} строк)"
        body = "\n".join(f"{i}\t{line}" for i, line in enumerate(chunk, start))
        end = start + len(chunk) - 1
        if end < len(lines):
            body += f"\n…(показаны строки {start}–{end} из {len(lines)})"
        return body


class WriteFile(PathTool):
    name = "write_file"
    description = "Создать или перезаписать файл целиком. Недостающие папки создаются."
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
        "expected_sha256": {"type": "string"}, "preview": {"type": "boolean"},
    }, "required": ["path", "content"], "additionalProperties": False}

    def _run(self, args, ctx):
        path = resolve(ctx, args["path"])
        if path.is_dir():
            raise ToolError(f"{args['path']}: это папка")
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        before = path.read_text(encoding='utf-8') if existed else ''
        if args.get('preview'):
            return diff(before, args['content'], args['path'])
        safe_write(path, args['content'], args, ctx)
        return f"{'Перезаписан' if existed else 'Создан'} {rel(ctx, path)} ({len(args['content'].encode())} байт)"


class EditFile(PathTool):
    name = "edit_file"
    description = ("Заменить фрагмент файла: old_string должен встречаться ровно один раз "
                   "(или укажи replace_all). Сначала прочитай файл, копируй фрагмент точно, с отступами.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "old_string": {"type": "string", "minLength": 1},
        "new_string": {"type": "string"},
        "replace_all": {"type": "boolean"},
        "expected_sha256": {"type": "string"}, "preview": {"type": "boolean"},
    }, "required": ["path", "old_string", "new_string"], "additionalProperties": False}

    def _run(self, args, ctx):
        path = resolve(ctx, args["path"])
        original_hash = digest(path)
        text = read_text(path, ctx.settings["files"]["max_read_bytes"])
        count = text.count(args["old_string"])
        if count == 0:
            raise ToolError("old_string не найден в файле")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(f"old_string встречается {count} раз — уточни фрагмент или укажи replace_all")
        new = text.replace(args["old_string"], args["new_string"], -1 if args.get("replace_all") else 1)
        if args.get('preview'):
            return diff(text, new, args['path'])
        safe_write(path, new, {'expected_sha256': original_hash, **args}, ctx)
        return f"Изменён {rel(ctx, path)}: замен — {count if args.get('replace_all') else 1}"


class ListDir(PathTool):
    plan_safe = True
    name = "list_dir"
    default_permission = "allow"
    description = "Список файлов и папок (папки — с `/` на конце, у файлов — размер)."
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "по умолчанию — рабочая папка"},
    }, "additionalProperties": False}

    def _run(self, args, ctx):
        path = resolve(ctx, args.get("path", ""))
        if not path.is_dir():
            raise ToolError(f"{args.get('path')}: не папка")
        entries = sorted(islice(path.iterdir(), MAX_LIST+1), key=lambda p: (not p.is_dir(), p.name))
        lines = [f"{p.name}/" if p.is_dir() else f"{p.name}\t{p.stat().st_size}" for p in entries[:MAX_LIST]]
        if len(entries) > MAX_LIST:
            lines.append(f"…больше {MAX_LIST} записей, уточните папку")
        return "\n".join(lines) or "(пусто)"


class Glob(PathTool):
    plan_safe = True
    name = "glob"
    default_permission = "allow"
    description = "Найти файлы по шаблону (`**/*.py`, `src/*.md`). Пропускает .git, node_modules, venv."
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "minLength": 1},
        "path": {"type": "string", "description": "откуда искать, по умолчанию — рабочая папка"},
    }, "required": ["pattern"], "additionalProperties": False}

    def _run(self, args, ctx):
        base = resolve(ctx, args.get("path", ""))
        root = ctx.workspace.resolve()
        found = []
        for p in base.glob(args["pattern"]):
            parts = p.relative_to(base).parts
            if any(part in SKIP_DIRS for part in parts[:-1]) or not p.resolve().is_relative_to(root):
                continue
            found.append(rel(ctx, p) + ("/" if p.is_dir() else ""))
            if len(found) > MAX_LIST:
                break
        found.sort()
        if len(found) > MAX_LIST:
            return "\n".join(found[:MAX_LIST]) + f"\n…(больше {MAX_LIST}, уточни шаблон)"
        return "\n".join(found) or "ничего не найдено"


class Grep(PathTool):
    plan_safe = True
    name = "grep"
    default_permission = "allow"
    description = ("Поиск по содержимому файлов регулярным выражением (Python re). "
                   "Возвращает `файл:строка:текст`. include — шаблон имён файлов (`*.py`).")
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "minLength": 1},
        "path": {"type": "string"},
        "include": {"type": "string"},
        "ignore_case": {"type": "boolean"},
    }, "required": ["pattern"], "additionalProperties": False}

    def _run(self, args, ctx):
        try:
            rx = re.compile(args["pattern"], re.IGNORECASE if args.get("ignore_case") else 0)
        except re.error as exc:
            raise ToolError(f"неверное регулярное выражение: {exc}") from None
        base = resolve(ctx, args.get("path", ""))
        limit = ctx.settings["files"]["max_read_bytes"]
        files = [base] if base.is_file() else self._walk(base)
        out = []
        for index, f in enumerate(files):
            if index >= ctx.settings['files'].get('max_scan_files', 1000):
                out.append('…достигнут files.max_scan_files; уточните папку')
                break
            if protected(f):
                continue
            if args.get("include") and not fnmatch(f.name, args["include"]):
                continue
            try:
                if f.stat().st_size > limit:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{rel(ctx, f)}:{n}:{line[:300]}")
                    if len(out) >= MAX_MATCHES:
                        return "\n".join(out) + f"\n…(показаны первые {MAX_MATCHES} совпадений)"
        return "\n".join(out) or "совпадений нет"

    @staticmethod
    def _walk(base: Path):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if not p.is_symlink():
                    yield p


TOOLS = [ReadFile, WriteFile, EditFile, ListDir, Glob, Grep]


def diff(before, after, name):
    return ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile=name, tofile=name)) or '(нет изменений)'


def safe_write(path, content, args, ctx):
    if len(content.encode()) > ctx.settings['files']['max_read_bytes']:
        raise ToolError('слишком большой результат записи')
    path.parent.mkdir(parents=True, exist_ok=True)
    locks = Path(tempfile.gettempdir()) / f'gwagent-locks-{os.getuid()}'
    locks.mkdir(mode=0o700, exist_ok=True)
    if locks.is_symlink() or locks.stat().st_uid != os.getuid() or locks.stat().st_mode & 0o077:
        raise ToolError('небезопасный каталог блокировок')
    lock_path = locks / hashlib.sha256(str(path).encode()).hexdigest()
    with file_lock(lock_path):
        expected = args.get('expected_sha256', ctx.extra.get('read_hashes', {}).get(str(path)))
        if expected is not None and digest(path) != expected:
            raise ToolError('файл изменён после чтения; прочитайте его заново')
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        atomic_write(path, content.encode(), mode)
        ctx.extra.setdefault('read_hashes', {})[str(path)] = digest(path)


def _file_worker(pipe, name, args, workspace, files, protected_paths):
    try:
        for p in protected_paths:
            protect(p)
        result = {'grep': Grep, 'glob': Glob, 'list_dir': ListDir}[name]()._run(args, ToolContext(Path(workspace), {'files': files}))
        # Не допускаем огромного IPC-сообщения, блокирующего отмену в родителе.
        pipe.send((True, result[:100000]))
    except Exception as exc:
        pipe.send((False, str(exc)))
    finally:
        pipe.close()
