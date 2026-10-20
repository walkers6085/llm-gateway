"""Сессии агента в SQLite.

Сервер держит их в той же базе, что и токены (`data/gateway.db`), CLI — в
`$GWAGENT_HOME/sessions.db`. Сообщения хранятся в формате OpenAI
(`{"role": …, "content": …, "tool_calls": …, "tool_call_id": …}`) и отдаются модели как есть.
Схема и миграции — docs/wiki/sessions.md.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent.storage import private_database, synchronized

SCHEMA_VERSION = 7

MIGRATIONS = {
    7: "ALTER TABLE agent_sessions ADD COLUMN permissions_mode TEXT NOT NULL DEFAULT '';",
    6: """
    CREATE TABLE agent_inputs (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        request_id TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL);
    CREATE INDEX agent_inputs_pending ON agent_inputs(session_id,status,seq);
    """,
    5: "ALTER TABLE agent_sessions ADD COLUMN agent_mode TEXT NOT NULL DEFAULT 'build';",
    4: """
    ALTER TABLE agent_sessions ADD COLUMN title_auto INTEGER NOT NULL DEFAULT 0;
    UPDATE agent_sessions SET title='Новая сессия · ' || substr(id,1,6),title_auto=1 WHERE title='';
    CREATE TABLE agent_message_meta (session_id TEXT NOT NULL, seq INTEGER NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(session_id,seq), FOREIGN KEY(session_id,seq) REFERENCES agent_messages(session_id,seq) ON DELETE CASCADE);
    CREATE TABLE agent_checkpoints (request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        before_data BLOB, after_data BLOB, error TEXT NOT NULL DEFAULT '',parent_request_id TEXT NOT NULL DEFAULT '');
    CREATE TABLE agent_snapshot_blobs (hash TEXT PRIMARY KEY,data BLOB NOT NULL);
    CREATE TABLE agent_snapshot_refs (request_id TEXT NOT NULL REFERENCES agent_checkpoints(request_id) ON DELETE CASCADE,
        hash TEXT NOT NULL REFERENCES agent_snapshot_blobs(hash),PRIMARY KEY(request_id,hash));
    CREATE TABLE agent_reverts (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        ts REAL NOT NULL, from_seq INTEGER NOT NULL, files INTEGER NOT NULL, data TEXT NOT NULL);
    """,
    3: """
    ALTER TABLE agent_tool_runs ADD COLUMN request_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE agent_tool_runs ADD COLUMN span_id TEXT NOT NULL DEFAULT '';
    CREATE TABLE agent_trace_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        event TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL
    );
    CREATE INDEX agent_trace_request ON agent_trace_events(request_id, seq);
    CREATE INDEX agent_trace_session ON agent_trace_events(session_id, seq);
    """,
    2: """
    ALTER TABLE agent_sessions ADD COLUMN connection_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE agent_sessions ADD COLUMN endpoint_hash TEXT NOT NULL DEFAULT '';
    ALTER TABLE agent_sessions ADD COLUMN summary TEXT NOT NULL DEFAULT '';
    ALTER TABLE agent_sessions ADD COLUMN context_seq INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE agent_turns (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        state TEXT NOT NULL, started REAL NOT NULL, finished REAL);
    """,
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

    connection_id: str = ""
    endpoint_hash: str = ""
    summary: str = ""
    context_seq: int = 0
    title_auto: bool = False
    agent_mode: str = "build"
    permissions_mode: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("id", "title", "profile", "model", "workspace", "allow_rules", "created", "updated", "connection_id", "endpoint_hash", "summary", "context_seq", "title_auto", "agent_mode", "permissions_mode")}


def _row(row: sqlite3.Row) -> Session:
    return Session(id=row["id"], owner=row["owner"], title=row["title"], profile=row["profile"],
                   model=row["model"], workspace=row["workspace"],
                   allow_rules=json.loads(row["allow_rules"]), created=row["created"], updated=row["updated"], connection_id=row["connection_id"],
                   endpoint_hash=row["endpoint_hash"], summary=row["summary"], context_seq=row["context_seq"], title_auto=bool(row["title_auto"]), agent_mode=row["agent_mode"], permissions_mode=row["permissions_mode"])


@synchronized
class SessionStore:
    def __init__(self, db_path: str | Path):
        self.path = str(db_path)
        self._lock = threading.RLock()
        private_database(db_path)
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
        self.db.execute('BEGIN IMMEDIATE')
        try:
            current = self.version()
            if current > SCHEMA_VERSION:
                raise ValueError('база создана более новой версией агента')
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for stmt in MIGRATIONS[version].split(';'):
                    if stmt.strip():
                        self.db.execute(stmt)
                self.db.execute("INSERT OR REPLACE INTO agent_meta VALUES ('schema_version', ?)", (str(version),))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def create(self, owner: str, *, title: str = "", profile: str = "", model: str = "",
               workspace: str = "", connection_id: str = "", endpoint_hash: str = "", agent_mode: str = "build") -> Session:
        if agent_mode not in ('build', 'plan'):
            raise ValueError('режим агента: build | plan')
        now = time.time()
        sid = secrets.token_hex(8)
        title_auto = not bool(title)
        title = title or initial_title(sid)
        self.db.execute(
            "INSERT INTO agent_sessions (id, owner, title, profile, model, workspace, created, updated) "
            "VALUES (?,?,?,?,?,?,?,?)", (sid, owner, title, profile, model, workspace, now, now))
        self.update(sid, connection_id=connection_id, endpoint_hash=endpoint_hash, title_auto=int(title_auto), agent_mode=agent_mode)
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
        allowed = {"title", "model", "workspace", "allow_rules", "connection_id", "endpoint_hash", "summary", "context_seq", "title_auto", "agent_mode", "permissions_mode"}
        if set(fields) - allowed:
            raise ValueError(f"нельзя менять: {', '.join(set(fields) - allowed)}")
        if fields.get('agent_mode', 'build') not in ('build', 'plan'):
            raise ValueError('режим агента: build | plan')
        if fields.get("permissions_mode", "") not in ("", "default", "allow_all", "read_only"):
            raise ValueError("режим разрешений: default | allow_all | read_only")
        if "allow_rules" in fields:
            fields["allow_rules"] = json.dumps(fields["allow_rules"], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE agent_sessions SET {sets}, updated=? WHERE id=?",
                        (*fields.values(), time.time(), session_id))

    def workspace_in_use(self, workspace: str) -> bool:
        return self.db.execute("SELECT 1 FROM agent_sessions WHERE workspace=? LIMIT 1", (workspace,)).fetchone() is not None

    def delete(self, session_id: str) -> None:
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute("DELETE FROM agent_sessions WHERE id=?", (session_id,))
            self.db.execute('DELETE FROM agent_snapshot_blobs WHERE hash NOT IN (SELECT hash FROM agent_snapshot_refs)')
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def enqueue_input(self, session_id, request_id, input_id, text):
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 65536:
            raise ValueError('уточнение должно содержать от 1 до 65536 байт текста')
        if not isinstance(input_id, str) or not 1 <= len(input_id) <= 80:
            raise ValueError('неверный идентификатор уточнения')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            existing = self.db.execute('SELECT * FROM agent_inputs WHERE id=?', (input_id,)).fetchone()
            if existing:
                if (existing['session_id'], existing['request_id'], existing['text']) != (session_id, request_id, text):
                    raise ValueError('идентификатор уточнения уже использован')
                self.db.execute('COMMIT')
                return dict(existing)
            pending = self.pending_inputs(session_id)
            if len(pending) >= 32 or sum(len(item['text'].encode()) for item in pending) + len(text.encode()) > 262144:
                raise ValueError('очередь уточнений заполнена; дождитесь их обработки')
            self.db.execute('INSERT INTO agent_inputs (id,session_id,request_id,text,created) VALUES (?,?,?,?,?)',
                            (input_id, session_id, request_id, text, time.time()))
            row = dict(self.db.execute('SELECT * FROM agent_inputs WHERE id=?', (input_id,)).fetchone())
            self.db.execute('COMMIT')
            return row
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def pending_inputs(self, session_id):
        return [dict(row) for row in self.db.execute("SELECT * FROM agent_inputs WHERE session_id=? AND status='pending' ORDER BY seq", (session_id,))]

    def reject_input(self, input_id):
        self.db.execute("UPDATE agent_inputs SET status='rejected' WHERE id=? AND status='pending'", (input_id,))

    def add_message(self, session_id: str, message: dict, metadata=None) -> int:
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_messages WHERE session_id=?",
                                  (session_id,)).fetchone()
            seq = row[0]
            self.db.execute("INSERT INTO agent_messages VALUES (?,?,?,?,?)",
                            (session_id, seq, message["role"], json.dumps(message, ensure_ascii=False), now))
            if metadata:
                self.db.execute("INSERT INTO agent_message_meta VALUES (?,?,?)", (session_id, seq, json.dumps(metadata, ensure_ascii=False)))
            if metadata and metadata.get('steering'):
                self.db.execute("UPDATE agent_inputs SET status='applied' WHERE id=? AND session_id=? AND status='pending'", (metadata['input_id'], session_id))
            self.db.execute("UPDATE agent_sessions SET updated=? WHERE id=?", (now, session_id))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return seq

    def messages(self, session_id: str) -> list[dict]:
        rows = self.db.execute("SELECT data FROM agent_messages WHERE session_id=? ORDER BY seq", (session_id,))
        return [json.loads(r["data"]) for r in rows]

    def timeline(self, session_id):
        return [{"seq": row["seq"], "message": json.loads(row["data"]), "metadata": json.loads(row["meta"] or '{}')}
                for row in self.db.execute("SELECT m.seq,m.data,x.data AS meta FROM agent_messages m LEFT JOIN agent_message_meta x USING(session_id,seq) WHERE m.session_id=? ORDER BY m.seq", (session_id,))]

    def revision(self, session_id):
        import hashlib
        return hashlib.sha256(json.dumps(self.timeline(session_id), sort_keys=True).encode()).hexdigest()

    def _pack_snapshot(self, data):
        import base64
        import hashlib
        import zlib
        if data is None:
            return None, []
        tree = json.loads(zlib.decompress(data))
        hashes = []
        for value in tree.values():
            if value['kind'] != 'file':
                continue
            raw = base64.b64decode(value.pop('data'))
            key = hashlib.sha256(raw).hexdigest()
            self.db.execute('INSERT OR IGNORE INTO agent_snapshot_blobs VALUES (?,?)', (key, zlib.compress(raw)))
            value['blob'] = key
            hashes.append(key)
        return zlib.compress(json.dumps(tree, ensure_ascii=False).encode()), hashes

    def _unpack_snapshot(self, data):
        import base64
        import hashlib
        import zlib
        if data is None:
            return None
        tree = json.loads(zlib.decompress(data))
        for value in tree.values():
            if value['kind'] != 'file':
                continue
            key = value.pop('blob')
            blob = self.db.execute('SELECT data FROM agent_snapshot_blobs WHERE hash=?', (key,)).fetchone()
            if blob is None:
                raise ValueError('снимок повреждён: отсутствует объект файла')
            raw = zlib.decompress(blob['data'])
            if hashlib.sha256(raw).hexdigest() != key:
                raise ValueError('снимок повреждён: не совпадает SHA-256')
            value['data'] = base64.b64encode(raw).decode()
        return zlib.compress(json.dumps(tree, ensure_ascii=False).encode())

    def checkpoint(self, session_id, request_id, *, before=None, after=None, error=""):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            before, before_refs = self._pack_snapshot(before)
            after, after_refs = self._pack_snapshot(after)
            parent = next((row['metadata'].get('request_id', '') for row in reversed(self.timeline(session_id))
                           if row['message']['role'] == 'user' and row['metadata'].get('request_id') != request_id), '')
            self.db.execute("INSERT INTO agent_checkpoints (request_id,session_id,before_data,after_data,error,parent_request_id) VALUES (?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET after_data=excluded.after_data,error=excluded.error",
                            (request_id, session_id, before, after, error, parent))
            for key in set(before_refs + after_refs):
                self.db.execute('INSERT OR IGNORE INTO agent_snapshot_refs VALUES (?,?)', (request_id, key))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def revert_checkpoints(self, session_id, seq):
        timeline = [row for row in self.timeline(session_id) if row['seq'] >= seq and row['message']['role'] == 'user']
        requests = list(dict.fromkeys(row['metadata'].get('checkpoint_id', row['metadata'].get('request_id', '')) for row in timeline))
        result = []
        for request in requests:
            row = self.db.execute("SELECT * FROM agent_checkpoints WHERE request_id=? AND session_id=?", (request, session_id)).fetchone()
            if row is None:
                raise ValueError('для старых сообщений нет снимков; можно изменить сообщение без отката файлов')
            result.append({**dict(row), 'before_data': self._unpack_snapshot(row['before_data']), 'after_data': self._unpack_snapshot(row['after_data'])})
        return result

    def rewind(self, session_id, seq, *, revision, files=False):
        if self.revision(session_id) != revision:
            raise ValueError('история изменилась; откройте /revert заново')
        timeline = self.timeline(session_id)
        selected = next((row for row in timeline if row['seq'] == seq and row['message']['role'] == 'user'), None)
        if selected is None:
            raise ValueError('выберите пользовательское сообщение')
        if self.db.execute("SELECT 1 FROM agent_turns WHERE session_id=? AND state='running'", (session_id,)).fetchone():
            raise ValueError('дождитесь завершения запроса перед откатом')
        session = self.get(session_id)
        if files and self.db.execute("SELECT 1 FROM agent_turns t JOIN agent_sessions s ON t.session_id=s.id WHERE s.workspace=? AND t.state='running'", (session.workspace,)).fetchone():
            raise ValueError('в рабочей папке выполняется другой запрос; дождитесь завершения')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            count = 0
            if files:
                from .checkpoints import restore
                count = restore(session.workspace, self.revert_checkpoints(session_id, seq))
            archived = [row for row in timeline if row['seq'] >= seq]
            self.db.execute("INSERT INTO agent_reverts (session_id,ts,from_seq,files,data) VALUES (?,?,?,?,?)",
                            (session_id, time.time(), seq, int(files), json.dumps(archived, ensure_ascii=False)))
            self.db.execute("UPDATE agent_inputs SET status='reverted' WHERE session_id=? AND status='pending'", (session_id,))
            self.db.execute("DELETE FROM agent_messages WHERE session_id=? AND seq>=?", (session_id, seq))
            self.update(session_id, summary='', context_seq=0, **({'title': initial_title(session_id)} if session.title_auto and not any(row['seq'] < seq and row['message']['role'] == 'user' for row in timeline) else {}))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        return {"text": selected['metadata'].get('user_input', selected['message'].get('content') or ''), "files": count}

    def record_tool(self, session_id: str, call_id: str, tool: str, args: dict, decision: str,
                    ok: bool, ms: int, *, request_id="", span_id="") -> None:
        self.db.execute("INSERT INTO agent_tool_runs (session_id,call_id,tool,args,decision,ok,ms,ts,request_id,span_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (session_id, call_id, tool, json.dumps(args, ensure_ascii=False), decision,
                         int(ok), ms, time.time(), request_id, span_id))

    def tool_runs(self, session_id: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM agent_tool_runs WHERE session_id=? ORDER BY ts", (session_id,))
        return [{**dict(r), "args": json.loads(r["args"]), "ok": bool(r["ok"])} for r in rows]

    def trace(self, request_id, span_id, parent_span_id, session_id, event, data):
        self.db.execute("INSERT INTO agent_trace_events (request_id,span_id,parent_span_id,session_id,event,ts,data) VALUES (?,?,?,?,?,?,?)",
                        (request_id, span_id, parent_span_id, session_id, event, time.time(), json.dumps(data, ensure_ascii=False)))

    def traces(self, session_id, request_id=None):
        sql = "SELECT * FROM agent_trace_events WHERE request_id IN (SELECT request_id FROM agent_trace_events WHERE session_id=?)"
        args = [session_id]
        if request_id:
            sql += " AND request_id=?"
            args.append(request_id)
        return [{**dict(row), "data": json.loads(row["data"])} for row in self.db.execute(sql + " ORDER BY seq", args)]

    def close(self):
        self.db.close()

    def repair_calls(self, session_id, reason="Прервано; действие не повторялось автоматически"):
        history = self.messages(session_id)
        start = next((i for i in range(len(history)-1, -1, -1) if history[i]["role"] == "assistant"), None)
        if start is None:
            return
        calls = history[start].get("tool_calls") or []
        completed = {m.get("tool_call_id") for m in history[start+1:] if m["role"] == "tool"}
        for call in calls:
            if call["id"] not in completed:
                self.add_message(session_id, {"role": "tool", "tool_call_id": call["id"], "content": reason})
                completed.add(call["id"])

    def begin_turn(self, session_id):
        self.repair_calls(session_id)
        self.db.execute("UPDATE agent_turns SET state='interrupted', finished=? WHERE session_id=? AND state='running'", (time.time(), session_id))
        turn_id = secrets.token_hex(16)
        self.db.execute("INSERT INTO agent_turns VALUES (?,?, 'running', ?, NULL)", (turn_id, session_id, time.time()))
        return turn_id

    def end_turn(self, turn_id, state):
        self.db.execute("UPDATE agent_turns SET state=?, finished=? WHERE id=?", (state, time.time(), turn_id))

    def planning_state(self, session_id):
        """История — источник состояния: откат сообщений откатывает и цель/задачи."""
        state = {'goal': '', 'todos': []}
        initial = False
        for row in self.timeline(session_id):
            message, metadata = row['message'], row['metadata']
            if message['role'] == 'user' and not initial:
                state['goal'] = metadata.get('user_input', message.get('content') or '')[:4000]
                initial = True
            update = metadata.get('planning')
            if message['role'] == 'tool' and isinstance(update, dict):
                if isinstance(update.get('goal'), str):
                    state['goal'] = update['goal']
                if isinstance(update.get('todos'), list):
                    state['todos'] = update['todos']
        return state

    def context_messages(self, session_id):
        session = self.get(session_id)
        rows = self.db.execute("SELECT seq,data FROM agent_messages WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()
        history = [json.loads(row['data']) for row in rows if row['seq'] > session.context_seq]
        # После сжатия внутри хода сохраняем точный запрос перед целой парой call/result.
        if session.context_seq and (not history or history[0]['role'] != 'user'):
            users = [json.loads(row['data']) for row in rows if row['seq'] <= session.context_seq and json.loads(row['data'])['role'] == 'user']
            if users:
                history.insert(0, users[-1])
        return history

    def compaction_candidate(self, session_id, keep_turns=1):
        session = self.get(session_id)
        rows = self.db.execute("SELECT seq,data FROM agent_messages WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()
        active = [row for row in rows if row['seq'] > session.context_seq]
        messages = [json.loads(row['data']) for row in active]
        starts = [i for i, message in enumerate(messages) if message['role'] == 'user']
        cut = starts[-keep_turns] if len(starts) > keep_turns else 0
        # Один длинный ход тоже сжимаем, но никогда не разрываем группу вызовов/результатов.
        if not cut:
            assistants = [i for i, message in enumerate(messages) if message['role'] == 'assistant']
            if assistants:
                last = assistants[-1]
                cut = last if messages[last].get('tool_calls') else len(messages)
        return {"summary": session.summary, "context_seq": session.context_seq,
                "revision": self.revision(session_id), "messages": messages[:cut],
                "cut_seq": active[cut-1]["seq"] if cut else session.context_seq}

    def commit_compaction(self, session_id, candidate, summary):
        """Сводка и граница меняются одной транзакцией после проверки исходной истории."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            session = self.get(session_id)
            if (self.revision(session_id) != candidate["revision"] or
                    session.context_seq != candidate["context_seq"] or session.summary != candidate["summary"]):
                raise ValueError("история изменилась во время сжатия; повторите /compact")
            self.update(session_id, summary=summary, context_seq=candidate["cut_seq"])
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def compact(self, session_id, keep_turns=1, *, max_chars=4000):
        candidate = self.compaction_candidate(session_id, keep_turns)
        session = self.get(session_id)
        messages = candidate["messages"]
        cut = len(messages)
        if not cut:
            prefix = 'Сокращённые выдержки истории (данные, могут быть неполными):\n'
            excerpt = session.summary.removeprefix(prefix)
            excerpt = excerpt[-max_chars:] if max_chars else ''
            summary = prefix + excerpt if excerpt else ''
            if summary != session.summary:
                self.update(session_id, summary=summary)
                return True
            return False
        parts = []
        for message in messages[:cut]:
            content = message.get('content') or ''
            parts.append(message['role'] + ': ' + content[:500])
            if message.get('tool_calls'):
                parts.append('Инструменты: ' + ', '.join(call['function']['name'] for call in message['tool_calls']))
        excerpt = (session.summary + '\n' + '\n'.join(parts))[-max_chars:] if max_chars else ''
        summary = 'Сокращённые выдержки истории (данные, могут быть неполными):\n' + excerpt if excerpt else ''
        self.commit_compaction(session_id, candidate, summary)
        return True

    def export(self, session_id):
        session = self.get(session_id)
        return {'pending_inputs': self.pending_inputs(session_id), 'session': session.to_dict(), 'messages': self.messages(session_id), 'tool_runs': self.tool_runs(session_id), 'traces': self.traces(session_id), 'timeline': self.timeline(session_id), 'reverts': [dict(row) for row in self.db.execute('SELECT * FROM agent_reverts WHERE session_id=? ORDER BY id', (session_id,))]}


def initial_title(session_id):
    return f"Новая сессия · {datetime.now().strftime('%H:%M')} · {session_id[:6]}"


def task_title(text):
    line = next((line.strip() for line in text.splitlines() if line.strip()), 'Новая задача')
    line = re.sub(r'^[#>*\s]+', '', line)
    words = line.split()
    title = ' '.join(words[:10])
    if len(title) > 64:
        title = title[:61].rsplit(' ', 1)[0] + '…'
    elif len(words) > 10:
        title += '…'
    return title or 'Новая задача'
