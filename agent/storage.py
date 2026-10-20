"""Атомарные приватные файлы и синхронизация локальных хранилищ."""

import fcntl
import functools
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def digest(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


@contextmanager
def file_lock(path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('запись через симлинк запрещена')
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, data):
    atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode())


def private_database(path):
    if str(path) == ':memory:':
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)


def synchronized(cls):
    """Блокировка охватывает execute, fetch и всю транзакцию, не отдельный SQL."""
    for name, method in list(vars(cls).items()):
        if callable(method) and not name.startswith('__'):
            def wrap(fn):
                @functools.wraps(fn)
                def call(self, *args, **kwargs):
                    with self._lock:
                        return fn(self, *args, **kwargs)
                return call
            setattr(cls, name, wrap(method))
    return cls
