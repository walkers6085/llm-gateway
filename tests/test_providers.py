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


def test_builtin_glm_and_mistral_presets_are_independent_and_require_own_keys(tmp_path,monkeypatch):
    first=ProviderManager(tmp_path/'first/providers.json')
    second=ProviderManager(tmp_path/'second/providers.json')
    for name,env,model in [('glm','ZAI_API_KEY','glm-5.1'),('mistral','MISTRAL_API_KEY','mistral-small-latest')]:
        monkeypatch.delenv(env,raising=False)
        assert first.get(name)['model']==model
        with pytest.raises(ValueError,match=env):
            first.connection(name)
        monkeypatch.setenv(env,'test-'+name+'-preset-secret')
        connection=first.connection(name)
        assert connection.model==model and connection.token=='test-'+name+'-preset-secret'
        assert 'preset-secret' not in repr(connection)
    first.config['providers']['glm']['models'].append('local-only')
    assert second.get('glm')['models']==['glm-5.1','glm-4.5-flash','glm-4.7-flash']
    assert second.get('glm')['tags']=={'glm-4.5-flash':['free'],'glm-4.7-flash':['free']}
    assert not first.path.exists() and not second.path.exists()
    assert not first.selected


def test_builtin_preset_can_use_stored_auth_without_environment(tmp_path,monkeypatch,capsys):
    manager=ProviderManager(tmp_path/'providers.json')
    monkeypatch.delenv('ZAI_API_KEY',raising=False)
    manager.connect('glm',read=lambda _:'bearer',password=lambda _:'stored-glm-test-secret')
    manager.use('glm')
    loaded=ProviderManager(manager.path)
    assert loaded.connection().token=='stored-glm-test-secret'
    assert loaded.connection().model=='glm-5.1'
    assert loaded.selected
    assert 'stored-glm-test-secret' not in manager.path.read_text()
    assert main(['--file',str(manager.path),'list'])==0
    assert 'stored-glm-test-secret' not in capsys.readouterr().out


def test_tags_validate_merge_and_display(tmp_path, capsys):
    path = tmp_path / "providers.json"
    manager = ProviderManager(path)
    manager.add("pro", "https://example.org/v1", model="m1", tags={"m1": ["free"]})
    assert manager.get("pro")["tags"] == {"m1": ["free"]}
    assert main(["--file", str(path), "list"]) == 0
    assert "m1(free)" in capsys.readouterr().out
    assert main(["--file", str(path), "update", "pro", "--tag", "m1=shared", "--tag", "m2=free"]) == 0
    assert ProviderManager(path).get("pro")["tags"] == {"m1": ["free", "shared"], "m2": ["free"]}
    path.write_text(json.dumps({"default": "pro", "providers": {"pro": {
        "url": "https://example.org/v1", "token_env": "", "model": "", "models": [],
        "tags": {"m1": "not-a-list"}}}}))
    with pytest.raises(ValueError):
        ProviderManager(path)
    with pytest.raises(ValueError):
        manager.update("pro", tags={"m1": [1]})
    assert main(["--file", str(path), "update", "pro", "--tag", "m3"]) == 2
