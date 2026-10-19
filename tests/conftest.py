import ssl

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.tokens import TokenStore
from tests.fakes import SCRIPT, SEEN, ServerThread, make_pki, upstream


@pytest.fixture(scope="session")
def pki(tmp_path_factory):
    return make_pki(tmp_path_factory.mktemp("pki"))


@pytest.fixture(scope="session")
def upstream_url(pki):
    with ServerThread(upstream, ssl_certfile=pki["server_cert"], ssl_keyfile=pki["server_key"],
                      ssl_ca_certs=pki["ca"], ssl_cert_reqs=ssl.CERT_REQUIRED) as srv:
        yield f"https://localhost:{srv.port}/v1"


@pytest.fixture
def gateway_settings(tmp_path, pki, upstream_url):
    def _settings(**overrides):
        settings = Settings(upstream_base_url=upstream_url, client_cert=pki["client_cert"],
                            client_key=pki["client_key"], ca_bundle=[pki["ca"]],
                            db_path=str(tmp_path / "gw.db"), upstream_timeout=10)
        for k, v in overrides.items():
            setattr(settings, k, v)
        return settings
    return _settings


@pytest.fixture
def make(gateway_settings):
    """make(**поля Settings) → (TestClient шлюза, TokenStore)."""
    def _make(**overrides):
        settings = gateway_settings(**overrides)
        store = TokenStore(settings.db_path)
        return TestClient(create_app(settings, store)), store
    SEEN.clear()
    SCRIPT.clear()
    yield _make
    SCRIPT.clear()
