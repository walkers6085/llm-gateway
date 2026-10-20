"""Локальные учётные данные; файл 0600, без выдачи ключей модели и в журнал."""

import json
import os
from pathlib import Path

from .storage import file_lock, write_json

DEFAULT_FILE = '~/.config/gwagent/auth.json'
_SECRETS = set()
_PROTECTED = set()


def register(value):
    if isinstance(value, str) and value:
        _SECRETS.add(value)
    return value


def redact(text):
    for value in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(value, '[секрет скрыт]')
    return text


def protect(path):
    if path:
        _PROTECTED.add(Path(path).expanduser().resolve())


def protected(path):
    path = Path(path).resolve()
    return path in _PROTECTED or path == Path(DEFAULT_FILE).expanduser().resolve()


class Credentials:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_FILE).expanduser()
        protect(self.path)

    def read(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise ValueError('auth.json не может быть симлинком')
        if self.path.stat().st_mode & 0o077:
            raise ValueError('права auth.json должны быть 0600')
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict):
            raise ValueError('auth.json: ожидается объект')
        for value in data.values():
            if not isinstance(value, dict):
                raise ValueError('auth.json: неверная запись')
            kind = value.get('type', 'bearer' if 'token' in value else 'none')
            if kind not in ('bearer', 'basic', 'none'):
                raise ValueError('auth.json: неверный тип авторизации')
            if kind == 'bearer' and (not isinstance(value.get('token'), str) or not value['token']):
                raise ValueError('auth.json: пустой или неверный ключ')
            if kind == 'basic' and not all(isinstance(value.get(k), str) for k in ('username', 'password')):
                raise ValueError('auth.json: неверный логин/пароль')
            if 'key_password' in value and not isinstance(value['key_password'], str):
                raise ValueError('auth.json: неверный пароль TLS-ключа')
            for key in ('token', 'password', 'key_password'):
                register(value.get(key))
        return data

    def get(self, key):
        return self.read().get(key, {})

    def set(self, key, value):
        with file_lock(self.path):
            data = self.read()
            data[key] = value
            write_json(self.path, data)
        self.read()

    def delete(self, key):
        with file_lock(self.path):
            data = self.read()
            data.pop(key, None)
            write_json(self.path, data)


def sanitize(value):
    if isinstance(value, str): return redact(value)
    if isinstance(value, list): return [sanitize(v) for v in value]
    if isinstance(value, dict): return {k: sanitize(v) for k,v in value.items()}
    return value
