"""Настройки шлюза из окружения / .env."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if value and not os.path.isabs(value):
        value = str(ROOT / value)
    return value


def _paths(name: str) -> list[str]:
    """Несколько путей через запятую (например, корневой и промежуточный CA)."""
    out = []
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if part:
            out.append(part if os.path.isabs(part) else str(ROOT / part))
    return out


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    upstream_base_url: str = ""
    upstream_api_key: str = ""
    client_cert: str = ""
    client_key: str = ""
    client_key_password: str = ""
    ca_bundle: list[str] = field(default_factory=list)
    upstream_proxy: str = ""
    upstream_timeout: float = 300.0
    db_path: str = ""
    agent_config: str = ""
    max_agent_runs: int = 16
    max_request_body_bytes: int = 10 * 1024 * 1024
    model_max_connections: dict[str, int] = field(default_factory=dict)
    model_max_connections_default: int = 0
    cors_origins: list[str] = field(default_factory=list)
    prod: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            upstream_base_url=os.getenv("UPSTREAM_BASE_URL", "").rstrip("/"),
            upstream_api_key=os.getenv("UPSTREAM_API_KEY", ""),
            client_cert=_path("CLIENT_CERT", "certs/client.crt"),
            client_key=_path("CLIENT_KEY", "certs/client.key"),
            client_key_password=os.getenv("CLIENT_KEY_PASSWORD", ""),
            ca_bundle=_paths("CA_BUNDLE"),
            upstream_proxy=os.getenv("UPSTREAM_PROXY", "").strip(),
            upstream_timeout=float(os.getenv("UPSTREAM_TIMEOUT", "300")),
            db_path=_path("DB_PATH", "data/gateway.db"),
            agent_config=_path("AGENT_CONFIG", "config/agent.json"),
            max_agent_runs=int(os.getenv("MAX_AGENT_RUNS", "16")),
            max_request_body_bytes=int(os.getenv("MAX_REQUEST_BODY_BYTES", str(10 * 1024 * 1024))),
            model_max_connections=json.loads(os.getenv("MODEL_MAX_CONNECTIONS", "{}") or "{}"),
            model_max_connections_default=int(os.getenv("MODEL_MAX_CONNECTIONS_DEFAULT", "0")),
            cors_origins=json.loads(os.getenv("CORS_ALLOW_ORIGINS", "[]") or "[]"),
            prod=os.getenv("GATEWAY_MODE", "PROD").upper() == "PROD",
        )
