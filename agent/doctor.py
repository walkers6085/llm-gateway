"""Диагностика без запуска инструментов MCP и без изменения базы/настроек."""

import os
from contextlib import closing
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import httpx
from cryptography import x509

from .providers import Connection, ProviderManager
from .settings import expand


async def diagnose(settings, provider=None, providers_file=None, transport=None):
    checks = []
    def add(ok, message):
        checks.append({'ok': bool(ok), 'message': message})
    try:
        manager = ProviderManager(providers_file)
        if provider or providers_file or manager.selected:
            connection = manager.connection(provider, settings.get('model') or None)
        else:
            gw = settings['gateway']
            token = gw.get('token') or os.environ.get(gw['token_env'], '')
            if not token:
                raise ValueError(f"задайте {gw['token_env']} или подключитесь через gwproviders connect")
            connection = Connection('gateway-settings', gw['url'], settings['model'], token)
        kwargs = connection.http_kwargs()
        add(True, f'Настройки подключения {connection.name} корректны')
        if connection.tls.get('cert'):
            cert = x509.load_pem_x509_certificate(Path(connection.tls['cert']).read_bytes())
            left = (cert.not_valid_after_utc - datetime.now(timezone.utc)).days
            add(left >= 0, f'Сертификат: осталось {left} дней; для замены используйте gwproviders update')
        async with httpx.AsyncClient(base_url=connection.url.rstrip('/')+'/', timeout=10, trust_env=False,
                                     transport=transport, **kwargs) as client:
            response = await client.get('models')
            add(response.status_code == 200, f'API /models: HTTP {response.status_code}; проверьте URL, ключ и CA при ошибке')
            if response.status_code == 200:
                models = response.json().get('data')
                add(isinstance(models, list) and bool(models), 'Каталог моделей непустой; иначе задайте модель явно')
    except (ValueError, OSError, httpx.HTTPError) as exc:
        add(False, f'Подключение: {type(exc).__name__}; проверьте конфиг, учётные данные, сертификат и доступность API')
    db = expand(settings['sessions']['db'])
    if db.exists():
        try:
            with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro', uri=True)) as connection:
                add(connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'SQLite: проверка целостности')
            add(not db.stat().st_mode & 0o077, 'Права SQLite: нужны 0600; chmod 600 для файла БД')
        except sqlite3.Error:
            add(False, 'SQLite недоступна; проверьте путь и права')
    else:
        parent = next((p for p in [db.parent, *db.parents] if p.exists()), None)
        add(parent is not None and os.access(parent, os.W_OK), 'Каталог SQLite доступен для создания базы')
    for name, config in settings['mcp']['servers'].items():
        if config.get('type') == 'stdio' and config.get('enabled', True):
            add(bool(shutil.which(os.path.expandvars(config.get('command', '')))), f'MCP {name}: исполняемый файл; сервер диагностикой не запускается')
    return checks
