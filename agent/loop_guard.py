"""Ограниченное окно точных повторов действий и результатов, без хранения их текста."""

import hashlib
import json
from collections import deque


DEFAULTS = {"enabled": True, "window": 24, "repetitions": 3, "max_pattern_length": 4}


def check_window(config):
    if config['window'] < config['repetitions'] * config['max_pattern_length']:
        raise ValueError('loop_guard.window должен быть не меньше repetitions × max_pattern_length')


class LoopGuard:
    def __init__(self, config=None):
        self.config = {**DEFAULTS, **(config or {})}
        check_window(self.config)
        self.history = deque(maxlen=self.config['window'])
        self.reset()

    def reset(self):
        self.history.clear()
        self.corrected = False
        self.action = None

    def observe(self, name, args, error, content):
        if not self.config['enabled']:
            return None
        encoded = json.dumps([name, args, bool(error), content], sort_keys=True,
                             ensure_ascii=False, separators=(',', ':')).encode()
        self.history.append(hashlib.sha256(encoded).digest())
        rows = list(self.history)
        repetitions = self.config['repetitions']
        for size in range(1, min(self.config['max_pattern_length'], len(rows) // repetitions) + 1):
            if rows[-size * repetitions:] == rows[-size:] * repetitions:
                self.action = 'stopped' if self.corrected else 'corrected'
                self.corrected = True
                self.history.clear()
                return {'action': self.action, 'pattern_length': size, 'repetitions': repetitions}
        return None
