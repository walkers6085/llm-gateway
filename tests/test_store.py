"""Сессии в SQLite. См. docs/wiki/sessions.md."""

import sqlite3

from agent.store import SCHEMA_VERSION, SessionStore
from gateway.tokens import TokenStore


def test_create_get_list_owner_isolation(tmp_path):
    s = SessionStore(tmp_path / "a.db")
    a = s.create("alice", title="первая", model="alpha", workspace="/w")
    b = s.create("bob")
    assert s.get(a.id).title == "первая"
    assert s.get(a.id, owner="bob") is None
    assert [x.id for x in s.list("alice")] == [a.id]
    assert s.find(a.id[:6], owner="alice").id == a.id
    assert s.find(b.id[:6], owner="alice") is None


def test_messages_order_and_roundtrip(tmp_path):
    s = SessionStore(tmp_path / "a.db")
    sess = s.create("u")
    msgs = [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    for m in msgs:
        s.add_message(sess.id, m)
    assert s.messages(sess.id) == msgs
    assert s.get(sess.id).updated >= sess.updated


def test_update_and_delete_cascade(tmp_path):
    s = SessionStore(tmp_path / "a.db")
    sess = s.create("u")
    s.add_message(sess.id, {"role": "user", "content": "x"})
    s.record_tool(sess.id, "c1", "shell", {"command": "ls"}, "allow", True, 5)
    s.update(sess.id, title="новое", allow_rules=["shell(ls *)"])
    got = s.get(sess.id)
    assert (got.title, got.allow_rules) == ("новое", ["shell(ls *)"])
    assert s.tool_runs(sess.id)[0]["args"] == {"command": "ls"}
    s.delete(sess.id)
    assert s.get(sess.id) is None
    assert s.messages(sess.id) == [] and s.tool_runs(sess.id) == []


def test_migration_is_idempotent_and_shares_db_with_tokens(tmp_path):
    db = tmp_path / "gateway.db"
    tokens = TokenStore(str(db))
    tokens.create("alice")
    SessionStore(db).create("1")
    again = SessionStore(db)                  # повторное открытие не пересоздаёт таблицы
    assert again.version() == SCHEMA_VERSION
    assert len(again.list("1")) == 1
    assert len(tokens.all()) == 1
    tables = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tokens", "usage", "agent_sessions", "agent_messages", "agent_tool_runs", "agent_meta"} <= tables
