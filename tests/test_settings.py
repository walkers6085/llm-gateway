"""Настройки агента: слои, слияние, валидация, профили сервера. См. docs/wiki/settings.md."""

import json

import pytest

from agent import settings as st
from agent.schema import SchemaError, validate


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_defaults_are_valid_and_complete():
    st.check(st.DEFAULTS)
    assert "shell.timeout" in st.flat_keys()
    assert "permissions.mode" in st.flat_keys()


def test_layers_override_in_order(tmp_path):
    user = write(tmp_path / "user.json", {"max_steps": 5, "shell": {"timeout": 10}})
    write(tmp_path / "proj" / ".gwagent" / "settings.json", {"shell": {"timeout": 20}, "model": "alpha"})
    extra = write(tmp_path / "extra.json", {"model": "beta"})
    s = st.load(tmp_path / "proj", extra=extra, overrides={"max_steps": 7}, user_file=user)
    assert s["shell"]["timeout"] == 20          # проект главнее пользователя
    assert s["shell"]["executable"] == "/bin/bash"  # соседние ключи словаря сохраняются
    assert s["model"] == "beta"                 # --settings главнее проекта
    assert s["max_steps"] == 7                  # флаги главнее всех
    assert st.DEFAULTS["max_steps"] == 25       # умолчания не испорчены


def test_lists_replace_not_append(tmp_path):
    user = write(tmp_path / "u.json", {"tools": {"enabled": ["read_file"]}})
    assert st.load(user_file=user)["tools"]["enabled"] == ["read_file"]


def test_missing_layers_are_skipped(tmp_path):
    s = st.load(tmp_path, user_file=tmp_path / "nope.json")
    assert s == st.DEFAULTS


def test_errors_name_file_and_key(tmp_path):
    bad = write(tmp_path / "u.json", {"shell": {"timeout": "долго"}})
    with pytest.raises(st.SettingsError, match=r"u\.json: shell\.timeout: ожидается integer"):
        st.load(user_file=bad)
    unknown = write(tmp_path / "u2.json", {"sheel": {}})
    with pytest.raises(st.SettingsError, match="sheel: неизвестный ключ"):
        st.load(user_file=unknown)
    broken = tmp_path / "u3.json"
    broken.write_text("{нет")
    with pytest.raises(st.SettingsError, match="неверный JSON"):
        st.load(user_file=broken)
    with pytest.raises(st.SettingsError, match="не найден"):
        st.load(user_file=tmp_path / "nope.json", extra=tmp_path / "missing.json")


def test_mcp_and_hooks_validation(tmp_path):
    ok = {"mcp": {"servers": {"fs": {"type": "stdio", "command": "x", "args": ["-v"]}}},
          "hooks": {"PreToolUse": [{"matcher": "shell", "command": "echo"}]}}
    st.check(ok)
    with pytest.raises(st.SettingsError, match=r"mcp\.servers\.fs\.type"):
        st.check({"mcp": {"servers": {"fs": {"type": "ftp"}}}})
    with pytest.raises(st.SettingsError, match=r"hooks\.Pre: неизвестный ключ"):
        st.check({"hooks": {"Pre": []}})
    with pytest.raises(st.SettingsError, match=r"hooks\.Stop\[0\]\.command: обязательное поле"):
        st.check({"hooks": {"Stop": [{}]}})


def test_profiles(tmp_path):
    assert set(st.load_profiles(tmp_path / "none.json")) == {"default"}
    path = write(tmp_path / "agent.json", {"profiles": {
        "default": {},
        "readonly": {"tools": {"enabled": ["read_file"]}, "permissions": {"mode": "read_only"}},
    }})
    profiles = st.load_profiles(path)
    assert profiles["readonly"]["tools"]["enabled"] == ["read_file"]
    assert profiles["readonly"]["shell"]["timeout"] == 120
    write(path, {"profiles": {}})
    with pytest.raises(st.SettingsError, match="profiles"):
        st.load_profiles(path)
    write(path, {"profiles": {"x": {}}, "extra": 1})
    with pytest.raises(st.SettingsError, match="extra"):
        st.load_profiles(path)


def test_expand(tmp_path, monkeypatch):
    monkeypatch.setenv("GWX", "sub")
    assert st.expand("$GWX/a", tmp_path) == tmp_path / "sub" / "a"
    assert str(st.expand("~/x")).startswith("/")


@pytest.mark.parametrize("value,schema,msg", [
    (True, {"type": "integer"}, "ожидается integer"),
    (5, {"type": "integer", "maximum": 3}, "не больше 3"),
    ("c", {"enum": ["a", "b"]}, "допустимые"),
    ([1, "x"], {"type": "array", "items": {"type": "integer"}}, r"\[1\]"),
    ({}, {"type": "object", "required": ["path"]}, "path: обязательное"),
])
def test_schema_errors(value, schema, msg):
    with pytest.raises(SchemaError, match=msg):
        validate(value, schema)


def test_schema_accepts_union_and_extra():
    validate(None, {"type": ["number", "null"]})
    validate({"a": 1, "b": 2}, {"type": "object", "properties": {"a": {"type": "integer"}}})


def test_example_server_config_is_valid():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    profiles = st.load_profiles(root / "config" / "agent.json.example")
    assert {"default", "readonly", "power"} <= set(profiles)
    assert profiles["readonly"]["permissions"]["mode"] == "read_only"
