"""Инструмент shell. См. docs/wiki/tools/shell.md."""

import time

import pytest

from agent.settings import DEFAULTS, merge
from agent.tools.base import ToolContext
from agent.tools.shell import Shell


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace=tmp_path, session_id="s1",
                       settings=merge(DEFAULTS, {"shell": {"env": {"EXTRA": "1"}, "max_timeout": 2},
                                                 "max_tool_output_chars": 200}))


async def test_runs_in_workspace_with_exit_code(ctx, tmp_path):
    res = await Shell().run({"command": "pwd; echo ошибка >&2; exit 3"}, ctx)
    assert res.is_error
    assert res.content.splitlines() == ["exit code: 3", str(tmp_path), "ошибка"]
    ok = await Shell().run({"command": "true"}, ctx)
    assert not ok.is_error and ok.content == "exit code: 0"


async def test_env_is_filtered(ctx, monkeypatch):
    monkeypatch.setenv("GW_TOKEN", "gw-secret")
    res = await Shell().run({"command": "echo token=$GW_TOKEN extra=$EXTRA s=$GWAGENT_SESSION; test -n \"$PATH\""}, ctx)
    assert "token= extra=1 s=s1" in res.content and not res.is_error


async def test_timeout_kills_process_group(ctx):
    started = time.monotonic()
    res = await Shell().run({"command": "echo начало; sleep 30 & sleep 30", "timeout": 60}, ctx)
    assert time.monotonic() - started < 6          # timeout урезан до max_timeout=2
    assert res.is_error and "Таймаут 2 с" in res.content and "начало" in res.content


async def test_output_truncated(ctx):
    res = await Shell().run({"command": "yes x | head -c 5000"}, ctx)
    assert "обрезано" in res.content and len(res.content) < 400


def test_subject():
    assert Shell().subject({"command": "  git status "}) == "git status"
    assert Shell().always_rule({"command": "make"}) == "shell(make)"
