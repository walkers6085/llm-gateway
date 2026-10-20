"""Ограниченные снимки workspace и проверяемый откат локальных файлов."""

import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import zlib

from .credentials import protected
from .storage import atomic_write, file_lock

SKIP = {'.git', 'node_modules', 'venv', '.venv', '__pycache__', '.pytest_cache', '.mypy_cache', '.cache'}
MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 5000


def entry(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return {'kind': 'link', 'target': os.readlink(path)}
    if stat.S_ISDIR(info.st_mode):
        return {'kind': 'dir', 'mode': mode}
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'снимок не поддерживает специальный файл: {path.name}')
    if info.st_size > MAX_BYTES:
        raise ValueError(f'файл превышает лимит снимка: {path.name}')
    data = path.read_bytes()
    after = path.lstat()
    if (info.st_ino, info.st_mtime_ns, info.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
        raise ValueError(f'файл меняется во время снимка: {path.name}')
    return {'kind': 'file', 'mode': mode, 'data': base64.b64encode(data).decode()}


def _walk_error(error):
    raise error


def capture(workspace, excluded=()):
    root = Path(workspace).resolve()
    excluded = {str(Path(p).resolve()) for p in excluded}
    result, size = {}, 0
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=_walk_error):
        dirs[:] = sorted(name for name in dirs if name not in SKIP)
        for name in [*dirs, *sorted(files)]:
            if name in SKIP:
                continue
            path = Path(directory) / name
            if str(path.resolve()) in excluded or protected(path):
                if name in dirs:
                    dirs.remove(name)
                continue
            value = entry(path)
            if value is None:
                raise ValueError('файл исчез во время снимка')
            result[str(path.relative_to(root))] = value
            size += len(value.get('data', '')) * 3 // 4
            if len(result) > MAX_ENTRIES or size > MAX_BYTES:
                raise ValueError('workspace превышает лимит снимка: 5000 записей / 32 МиБ')
    return zlib.compress(json.dumps(result, ensure_ascii=False).encode())


def unpack(data):
    return json.loads(zlib.decompress(data))


def checked_path(root, name):
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('небезопасный путь в снимке')
    target = root / relative
    for parent in target.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f'родитель пути стал симлинком: {name}')
    if protected(target):
        raise ValueError(f'защищённый файл: {name}')
    return target


def plan(workspace, checkpoints):
    root = Path(workspace).resolve()
    original, desired = {}, {}
    for checkpoint in reversed(checkpoints):
        if checkpoint['error'] or checkpoint['before_data'] is None or checkpoint['after_data'] is None:
            raise ValueError('для одного из запросов нет полного снимка; доступно изменение сообщения без отката файлов')
        before, after = unpack(checkpoint['before_data']), unpack(checkpoint['after_data'])
        for name in sorted(before.keys() | after.keys()):
            if before.get(name) == after.get(name):
                continue
            path = checked_path(root, name)
            if name not in original:
                original[name] = entry(path)
                desired[name] = original[name]
            if desired[name] != after.get(name):
                raise ValueError(f'конфликт отката: {name} изменён после действий модели; файлы не изменены')
            desired[name] = before.get(name)
    # Новые ручные файлы внутри созданной моделью папки должны сохраниться.
    for name, value in desired.items():
        if value is None and original[name] and original[name]['kind'] == 'dir':
            path = checked_path(root, name)
            if path.is_dir():
                for directory, dirs, files in os.walk(path, followlinks=False):
                    for child in [*dirs, *files]:
                        child_name = str((Path(directory) / child).relative_to(root))
                        if child_name not in desired or desired[child_name] is not None:
                            raise ValueError(f'конфликт отката: в {name} есть новые файлы')
    return original, desired


def apply_entries(root, original, desired):
    # Сначала удаляем файлы/ссылки и опустевшие папки, затем создаём прежнее дерево.
    for name in sorted(desired, key=lambda n: (-len(Path(n).parts), n)):
        path = checked_path(root, name)
        old, new = original[name], desired[name]
        if old is None or old == new:
            continue
        if old['kind'] == 'dir':
            if new is None or new['kind'] != 'dir':
                path.rmdir()
        elif new is None or new['kind'] != old['kind'] or old['kind'] == 'link':
            path.unlink()
    for name in sorted(desired, key=lambda n: (len(Path(n).parts), n)):
        value = desired[name]
        if value is None:
            continue
        path = checked_path(root, name)
        if value['kind'] == 'dir':
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(value['mode'])
        elif value['kind'] == 'link':
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.is_symlink():
                path.symlink_to(value['target'])
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, base64.b64decode(value['data']), value['mode'])


def restore(workspace, checkpoints):
    root = Path(workspace).resolve()
    original, desired = plan(root, checkpoints)
    locks = Path(tempfile.gettempdir()) / f'gwagent-locks-{os.getuid()}'
    locks.mkdir(mode=0o700, exist_ok=True)
    if locks.is_symlink() or locks.stat().st_uid != os.getuid() or locks.stat().st_mode & 0o077:
        raise ValueError('небезопасный каталог блокировок')
    with ExitStack() as stack:
        for name in sorted(desired):
            stack.enter_context(file_lock(locks / hashlib.sha256(str(root / name).encode()).hexdigest()))
        if any(entry(checked_path(root, name)) != value for name, value in original.items()):
            raise ValueError('файлы изменились во время подготовки отката')
        try:
            apply_entries(root, original, desired)
        except Exception:
            # Возвращаем уже применённые изменения, если запись оборвалась.
            now = {name: entry(checked_path(root, name)) for name in original}
            apply_entries(root, now, original)
            raise
    return len([name for name in desired if desired[name] != original[name]])
