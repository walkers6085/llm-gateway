"""Менеджер провайдеров: хранение, секреты и HTTP-каталог моделей."""

import json
import stat

import httpx
import pytest

from agent.providers import ProviderManager, main


def test_lifecycle_and_secrets(tmp_path, monkeypatch, capsys):
    path = tmp_path / "providers.json"
    manager = ProviderManager(path)
    manager.add("local", "http://localhost:8000/v1", model="model-a", models=["model-a", "vendor/model-b"])
    manager.add("remote", "https://example.org/v1", "TEST_PROVIDER_KEY", "vendor/model-b")
    manager.use("remote")
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-that-must-not-be-printed")
    manager = ProviderManager(path)
    connection = manager.connection()
    assert connection.name == "remote" and connection.model == "vendor/model-b"
    assert connection.token == "secret-that-must-not-be-printed"
    assert "secret-that" not in repr(connection) and "secret-that" not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert main(["--file", str(path), "list"]) == 0
    assert "secret-that" not in capsys.readouterr().out
    with pytest.raises(ValueError, match="сначала"):
        manager.remove("remote")
    manager.use("local", "vendor/model-b")
    manager.remove("remote")
    assert ProviderManager(path).connection().token == ""
    assert main(["--file", str(path), "models", "missing"]) == 2


@pytest.mark.parametrize("url", ["ftp://example.org", "http://user:secret@example.org/v1",
                                "http://example.org?api_key=secret", "http://example.org/#secret", "/v1"])
def test_invalid_url(tmp_path, url):
    manager = ProviderManager(tmp_path / "providers.json")
    with pytest.raises(ValueError):
        manager.add("invalid", url)
    assert not manager.path.exists()


def test_missing_key_and_invalid_config(tmp_path, monkeypatch):
    path = tmp_path / "providers.json"
    manager = ProviderManager(path)
    manager.add("test", "https://example.org/v1", "MISSING_PROVIDER_KEY")
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)
    with pytest.raises(ValueError, match="MISSING_PROVIDER_KEY"):
        manager.connection("test")
    path.write_text(json.dumps({"default": "missing", "providers": {}}))
    with pytest.raises(ValueError):
        ProviderManager(path)


async def test_models_request(tmp_path):
    manager = ProviderManager(tmp_path / "providers.json")
    manager.add("local", "http://example.org/custom/v1", model="model")
    def handler(req):
        assert req.url.path == "/custom/v1/models"
        assert "authorization" not in req.headers
        return httpx.Response(200, json={"data": [{"id": "vendor/model"}, {"id": 123}, {}]})
    assert await manager.models("local", transport=httpx.MockTransport(handler)) == ["vendor/model"]
    with pytest.raises(ValueError, match="HTTP 401"):
        await manager.models("local", transport=httpx.MockTransport(lambda req: httpx.Response(401, text="secret")))
