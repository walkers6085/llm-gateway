"""Сессии агента в SQLite.

Сервер держит их в той же базе, что и токены (`data/gateway.db`), CLI — в
`~/.local/share/gwagent/sessions.db`. Сообщения хранятся в формате OpenAI
(`{"role": …, "content": …, "tool_calls": …, "tool_call_id": …}`) и отдаются модели как есть.
Схема и миграции — docs/wiki/sessions.md.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

MIGRATIONS = {
    1: """
    CREATE TABLE agent_sessions (
        id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        profile TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        workspace TEXT NOT NULL DEFAULT '',
        allow_rules TEXT NOT NULL DEFAULT '[]',
        created REAL NOT NULL,
        updated REAL NOT NULL
    );
    CREATE INDEX agent_sessions_owner ON agent_sessions(owner, updated);
    CREATE TABLE agent_messages (
        session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        role TEXT NOT NULL,
        data TEXT NOT NULL,
        ts REAL NOT NULL,
        PRIMARY KEY (session_id, seq)
    );
    CREATE TABLE agent_tool_runs (
        session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        call_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        args TEXT NOT NULL,
        decision TEXT NOT NULL,
        ok INTEGER NOT NULL,
        ms INTEGER NOT NULL,
        ts REAL NOT NULL
    );
    CREATE INDEX agent_tool_runs_session ON agent_tool_runs(session_id, ts);
    """,
}


@dataclass
class Session:
    id: str
    owner: str
    title: str
    profile: str
    model: str
    workspace: str
    allow_rules: list[str] = field(default_factory=list)
    created: float = 0.0
    updated: float = 0.0

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("id", "title", "profile", "model", "workspace", "allow_rules", "created", "updated")}


def _row(row: sqlite3.Row) -> Session:
    return Session(id=row["id"], owner=row["owner"], title=row["title"], profile=row["profile"],
                   model=row["model"], workspace=row["workspace"],
                   allow_rules=json.loads(row["allow_rules"]), created=row["created"], updated=row["updated"])


class SessionStore:
    def __init__(self, db_path: str | Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    # Версия схемы — в своей таблице, а не в PRAGMA user_version: на сервере база общая с токенами.
    def version(self) -> int:
        self.db.execute("CREATE TABLE IF NOT EXISTS agent_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = self.db.execute("SELECT value FROM agent_meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else 0

    def migrate(self) -> None:
        current = self.version()
        for version in range(current + 1, SCHEMA_VERSION + 1):
            self.db.execute("BEGIN")
            try:
                for stmt in MIGRATIONS[version].split(";"):
                    if stmt.strip():
                        self.db.execute(stmt)
                self.db.execute("INSERT OR REPLACE INTO agent_meta VALUES ('schema_version', ?)", (str(version),))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def create(self, owner: str, *, title: str = "", profile: str = "", model: str = "",
               workspace: str = "") -> Session:
        now = time.time()
        sid = secrets.token_hex(8)
        self.db.execute(
            "INSERT INTO agent_sessions (id, owner, title, profile, model, workspace, created, updated) "
            "VALUES (?,?,?,?,?,?,?,?)", (sid, owner, title, profile, model, workspace, now, now))
        return self.get(sid)

    def get(self, session_id: str, owner: str | None = None) -> Session | None:
        """С `owner` возвращает сессию только её владельцу (чужая — как несуществующая)."""
        sql, args = "SELECT * FROM agent_sessions WHERE id=?", [session_id]
        if owner is not None:
            sql += " AND owner=?"
            args.append(owner)
        row = self.db.execute(sql, args).fetchone()
        return _row(row) if row else None

    def find(self, prefix: str, owner: str | None = None) -> Session | None:
        """По полному id или однозначному префиксу (удобно в CLI: `/resume 3fa2`)."""
        sql, args = "SELECT * FROM agent_sessions WHERE id LIKE ?", [prefix + "%"]
        if owner is not None:
            sql += " AND owner=?"
            args.append(owner)
        rows = self.db.execute(sql, args).fetchall()
        return _row(rows[0]) if len(rows) == 1 else None

    def list(self, owner: str, limit: int = 50) -> list[Session]:
        rows = self.db.execute("SELECT * FROM agent_sessions WHERE owner=? ORDER BY updated DESC LIMIT ?",
                               (owner, limit))
        return [_row(r) for r in rows]

    def update(self, session_id: str, **fields) -> None:
        allowed = {"title", "model", "workspace", "allow_rules"}
        if set(fields) - allowed:
            raise ValueError(f"нельзя менять: {', '.join(set(fields) - allowed)}")
        if "allow_rules" in fields:
            fields["allow_rules"] = json.dumps(fields["allow_rules"], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE agent_sessions SET {sets}, updated=? WHERE id=?",
                        (*fields.values(), time.time(), session_id))

    def delete(self, session_id: str) -> None:
        self.db.execute("DELETE FROM agent_sessions WHERE id=?", (session_id,))

    def add_message(self, session_id: str, message: dict) -> int:
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_messages WHERE session_id=?",
                                  (session_id,)).fetchone()
            seq = row[0]
            self.db.execute("INSERT INTO agent_messages VALUES (?,?,?,?,?)",
                            (session_id, seq, message["role"], json.dumps(message, ensure_ascii=False), now))
            self.db.execute("UPDATE agent_sessions SET updated=? WHERE id=?", (now, session_id))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return seq

    def messages(self, session_id: str) -> list[dict]:
        rows = self.db.execute("SELECT data FROM agent_messages WHERE session_id=? ORDER BY seq", (session_id,))
        return [json.loads(r["data"]) for r in rows]

    def record_tool(self, session_id: str, call_id: str, tool: str, args: dict, decision: str,
                    ok: bool, ms: int) -> None:
        self.db.execute("INSERT INTO agent_tool_runs VALUES (?,?,?,?,?,?,?,?)",
                        (session_id, call_id, tool, json.dumps(args, ensure_ascii=False), decision,
                         int(ok), ms, time.time()))

    def tool_runs(self, session_id: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM agent_tool_runs WHERE session_id=? ORDER BY ts", (session_id,))
        return [{**dict(r), "args": json.loads(r["args"]), "ok": bool(r["ok"])} for r in rows]
