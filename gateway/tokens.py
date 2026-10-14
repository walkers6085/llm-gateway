"""Пользовательские токены и учёт расхода (SQLite).

В базе хранится только sha256 токена и короткий префикс для опознания;
сам токен показывается один раз при выдаче.
"""

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

TOKEN_PREFIX = "gw-"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL,
    revoked REAL,
    models TEXT NOT NULL DEFAULT '',
    rpm INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
    token_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    path TEXT NOT NULL,
    model TEXT,
    status INTEGER NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS usage_token_ts ON usage(token_id, ts);
"""


@dataclass(frozen=True)
class Token:
    id: int
    name: str
    prefix: str
    created: float
    expires: float | None
    revoked: float | None
    models: tuple[str, ...]
    rpm: int

    @property
    def active(self) -> bool:
        return self.revoked is None and (self.expires is None or self.expires > time.time())

    def allows(self, model: str | None) -> bool:
        return not self.models or (model is not None and model in self.models)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_to_token(row: sqlite3.Row) -> Token:
    return Token(
        id=row["id"],
        name=row["name"],
        prefix=row["prefix"],
        created=row["created"],
        expires=row["expires"],
        revoked=row["revoked"],
        models=tuple(m for m in row["models"].split(",") if m),
        rpm=row["rpm"],
    )


class TokenStore:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def create(self, name: str, days: float | None = None, models: list[str] | None = None,
               rpm: int = 0) -> tuple[str, Token]:
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = time.time()
        expires = now + days * 86400 if days else None
        cur = self.db.execute(
            "INSERT INTO tokens (name, hash, prefix, created, expires, models, rpm) VALUES (?,?,?,?,?,?,?)",
            (name, hash_token(raw), raw[:10], now, expires, ",".join(models or []), rpm),
        )
        return raw, self.get(cur.lastrowid)

    def get(self, token_id: int) -> Token:
        row = self.db.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
        return _row_to_token(row)

    def lookup(self, raw: str) -> Token | None:
        row = self.db.execute("SELECT * FROM tokens WHERE hash=?", (hash_token(raw),)).fetchone()
        return _row_to_token(row) if row else None

    def find(self, name_or_prefix: str) -> Token | None:
        row = self.db.execute(
            "SELECT * FROM tokens WHERE name=? OR prefix=? OR prefix LIKE ?",
            (name_or_prefix, name_or_prefix, name_or_prefix + "%"),
        ).fetchall()
        return _row_to_token(row[0]) if len(row) == 1 else None

    def all(self) -> list[Token]:
        return [_row_to_token(r) for r in self.db.execute("SELECT * FROM tokens ORDER BY id")]

    def revoke(self, token_id: int) -> None:
        self.db.execute("UPDATE tokens SET revoked=? WHERE id=? AND revoked IS NULL", (time.time(), token_id))

    def record(self, token_id: int, path: str, model: str | None, status: int,
               prompt_tokens: int = 0, completion_tokens: int = 0, ms: int = 0) -> None:
        self.db.execute(
            "INSERT INTO usage VALUES (?,?,?,?,?,?,?,?)",
            (token_id, time.time(), path, model, status, prompt_tokens, completion_tokens, ms),
        )

    def usage(self, token_id: int | None = None, days: float = 30) -> list[sqlite3.Row]:
        since = time.time() - days * 86400
        sql = (
            "SELECT t.name, u.model, COUNT(*) AS requests, SUM(u.status >= 400) AS errors, "
            "SUM(u.prompt_tokens) AS prompt_tokens, SUM(u.completion_tokens) AS completion_tokens "
            "FROM usage u JOIN tokens t ON t.id = u.token_id WHERE u.ts >= ?"
        )
        args: list = [since]
        if token_id is not None:
            sql += " AND u.token_id = ?"
            args.append(token_id)
        sql += " GROUP BY t.name, u.model ORDER BY t.name, requests DESC"
        return self.db.execute(sql, args).fetchall()
