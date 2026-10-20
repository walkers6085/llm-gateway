"""Доверие точному содержимому проектного конфига, до запуска хуков и MCP."""

import hashlib
import json
from pathlib import Path

from .storage import file_lock, write_json

DEFAULT_FILE = '~/.config/gwagent/trust.json'


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TrustStore:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_FILE).expanduser()

    def trusted(self, project, content=None):
        project = Path(project).resolve()
        config = project / '.gwagent/settings.json'
        if not config.is_file():
            return True
        values = json.loads(self.path.read_text()) if self.path.exists() else {}
        expected = hashlib.sha256(content).hexdigest() if content is not None else fingerprint(config)
        return values.get(str(project)) == expected

    def set(self, project, allow=True, expected=None):
        project = Path(project).resolve()
        with file_lock(self.path):
            values = json.loads(self.path.read_text()) if self.path.exists() else {}
            if allow:
                current = fingerprint(project / '.gwagent/settings.json')
                if expected is not None and current != expected:
                    raise ValueError('конфиг изменился во время подтверждения; просмотрите его заново')
                values[str(project)] = current
            else:
                values.pop(str(project), None)
            write_json(self.path, values)
