"""Файловые инструменты и границы рабочей папки. См. docs/wiki/tools/files.md."""

import os

import pytest

from agent.settings import DEFAULTS, merge
from agent.tools.base import ToolContext, ToolError
from agent.tools.files import EditFile, Glob, Grep, ListDir, ReadFile, WriteFile


@pytest.fixture
def ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ToolContext(workspace=ws, settings=merge(DEFAULTS, {"files": {"max_read_bytes": 1000}}))


async def test_write_read_with_numbers_and_ranges(ctx):
    msg = await WriteFile().run({"path": "sub/a.txt", "content": "один\nдва\nтри\n"}, ctx)
    assert msg.startswith("Создан sub/a.txt")
    assert await ReadFile().run({"path": "sub/a.txt"}, ctx) == "1\tодин\n2\tдва\n3\tтри"
    part = await ReadFile().run({"path": "sub/a.txt", "offset": 2, "limit": 1}, ctx)
    assert part.startswith("2\tдва") and "строки 2–2 из 3" in part
    assert "пусто" in await ReadFile().run({"path": "sub/a.txt", "offset": 10}, ctx)
    assert (await WriteFile().run({"path": "sub/a.txt", "content": "x"}, ctx)).startswith("Перезаписан")


async def test_read_errors(ctx):
    (ctx.workspace / "big").write_text("x" * 2000)
    (ctx.workspace / "bin").write_bytes(b"\x00\x01")
    for path, msg in [("missing", "не существует"), ("big", "больше 1000"), ("bin", "двоичный")]:
        with pytest.raises(ToolError, match=msg):
            await ReadFile().run({"path": path}, ctx)


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "a/../../x"])
async def test_paths_outside_workspace_rejected(ctx, path):
    with pytest.raises(ToolError, match="вне рабочей папки"):
        await WriteFile().run({"path": path, "content": "x"}, ctx)


async def test_symlink_escape_rejected(ctx, tmp_path):
    (tmp_path / "secret").write_text("тайна")
    os.symlink(tmp_path / "secret", ctx.workspace / "link")
    with pytest.raises(ToolError, match="вне рабочей папки"):
        await ReadFile().run({"path": "link"}, ctx)


async def test_edit(ctx):
    (ctx.workspace / "f.py").write_text("a = 1\nb = 1\n")
    with pytest.raises(ToolError, match="2 раз"):
        await EditFile().run({"path": "f.py", "old_string": "= 1", "new_string": "= 2"}, ctx)
    with pytest.raises(ToolError, match="не найден"):
        await EditFile().run({"path": "f.py", "old_string": "zzz", "new_string": ""}, ctx)
    await EditFile().run({"path": "f.py", "old_string": "a = 1", "new_string": "a = 3"}, ctx)
    msg = await EditFile().run({"path": "f.py", "old_string": "= ", "new_string": "== ", "replace_all": True}, ctx)
    assert "замен — 2" in msg
    assert (ctx.workspace / "f.py").read_text() == "a == 3\nb == 1\n"


async def test_list_glob_grep(ctx):
    ws = ctx.workspace
    (ws / "src").mkdir()
    (ws / "src" / "m.py").write_text("def hello():\n    return 'Hi'\n")
    (ws / "README.md").write_text("hello world\n")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "x.py").write_text("hello\n")
    listing = await ListDir().run({}, ctx)
    assert listing.splitlines()[0] == "node_modules/" and "README.md\t12" in listing
    assert await Glob().run({"pattern": "**/*.py"}, ctx) == "src/m.py"
    out = await Grep().run({"pattern": "HELLO", "ignore_case": True}, ctx)
    assert out.splitlines() == ["README.md:1:hello world", "src/m.py:1:def hello():"]
    assert await Grep().run({"pattern": "hello", "include": "*.md"}, ctx) == "README.md:1:hello world"
    assert await Grep().run({"pattern": "нет"}, ctx) == "совпадений нет"
    with pytest.raises(ToolError, match="регулярное"):
        await Grep().run({"pattern": "("}, ctx)


def test_subject_and_schema():
    assert ReadFile().subject({"path": "./docs/../a.md"}) == "a.md"
    with pytest.raises(ToolError, match="path: обязательное"):
        ReadFile().check_args({})
    with pytest.raises(ToolError, match="неизвестный ключ"):
        ReadFile().check_args({"path": "a", "mode": "rb"})
