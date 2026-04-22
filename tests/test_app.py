"""Tests for bearer-auth middleware and config loading precedence.

The auth middleware is exercised via Starlette's ASGI test transport so we
never need to start uvicorn.  Config tests drive `load_config` directly with
monkeypatched env vars and temporary JSON files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from mcp_audit_logger.__main__ import _auth_wrap
from mcp_audit_logger.config import DownstreamHttp, DownstreamStdio, load_config

# ------------------------------------------------------------------ auth helpers


async def _ok_handler(scope: Scope, receive: Receive, send: Send) -> None:
    """Stub ASGI handler that always returns 200 OK."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok", "more_body": False})


def _make_app(token: str | None) -> TestClient:
    """Build a minimal Starlette app gated by _auth_wrap and return a TestClient."""
    expected = f"Bearer {token}" if token else None
    app = Starlette(routes=[Mount("/mcp", app=_auth_wrap(expected, _ok_handler))])
    return TestClient(app, raise_server_exceptions=True)


# ------------------------------------------------------------------ auth tests


def test_auth_disabled_no_header() -> None:
    client = _make_app(None)
    assert client.get("/mcp").status_code == 200


def test_auth_disabled_ignores_any_header() -> None:
    client = _make_app(None)
    assert client.get("/mcp", headers={"Authorization": "Bearer whatever"}).status_code == 200


def test_auth_enabled_no_header_returns_401() -> None:
    client = _make_app("secret")
    assert client.get("/mcp").status_code == 401


def test_auth_enabled_wrong_token_returns_401() -> None:
    client = _make_app("secret")
    assert client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_auth_enabled_bearer_prefix_missing_returns_401() -> None:
    # Token without "Bearer " prefix is still invalid
    client = _make_app("secret")
    assert client.get("/mcp", headers={"Authorization": "secret"}).status_code == 401


def test_auth_enabled_correct_token_passes() -> None:
    client = _make_app("secret")
    assert client.get("/mcp", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_auth_401_body() -> None:
    client = _make_app("tok")
    resp = client.get("/mcp")
    assert b"Unauthorized" in resp.content


# ------------------------------------------------------------------ config tests


def test_config_defaults(tmp_path: Path) -> None:
    cfg = load_config()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8765
    assert cfg.mount_path == "/mcp"
    assert cfg.log_level == "INFO"
    assert cfg.max_payload_bytes == 64 * 1024
    assert cfg.downstream is None
    assert cfg.http_token is None


def test_config_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_HOST", "0.0.0.0")
    monkeypatch.setenv("AUDIT_PORT", "9000")
    monkeypatch.setenv("AUDIT_MOUNT_PATH", "/proxy")
    monkeypatch.setenv("AUDIT_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("AUDIT_MAX_PAYLOAD_BYTES", "1024")
    monkeypatch.setenv("AUDIT_HTTP_TOKEN", "mytoken")
    cfg = load_config()
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000
    assert cfg.mount_path == "/proxy"
    assert cfg.log_level == "DEBUG"
    assert cfg.max_payload_bytes == 1024
    assert cfg.http_token == "mytoken"


def test_config_json_file(tmp_path: Path) -> None:
    data: dict[str, Any] = {
        "host": "192.168.1.1",
        "port": 7777,
        "log_level": "WARNING",
    }
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(data))
    cfg = load_config(p)
    assert cfg.host == "192.168.1.1"
    assert cfg.port == 7777
    assert cfg.log_level == "WARNING"


def test_config_env_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"port": 7777}))
    monkeypatch.setenv("AUDIT_PORT", "9999")
    cfg = load_config(p)
    assert cfg.port == 9999


def test_config_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path.json")


def test_config_downstream_stdio_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_DOWNSTREAM_COMMAND", "python")
    monkeypatch.setenv("AUDIT_DOWNSTREAM_ARGS", "-m server")
    cfg = load_config()
    assert isinstance(cfg.downstream, DownstreamStdio)
    assert cfg.downstream.command == "python"
    assert cfg.downstream.args == ["-m", "server"]


def test_config_downstream_stdio_json_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_DOWNSTREAM_COMMAND", "uvx")
    monkeypatch.setenv("AUDIT_DOWNSTREAM_ARGS", '["mcp-server-fetch", "--flag"]')
    cfg = load_config()
    assert isinstance(cfg.downstream, DownstreamStdio)
    assert cfg.downstream.args == ["mcp-server-fetch", "--flag"]


def test_config_downstream_http_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_DOWNSTREAM_URL", "http://localhost:3000/mcp")
    cfg = load_config()
    assert isinstance(cfg.downstream, DownstreamHttp)
    assert cfg.downstream.url == "http://localhost:3000/mcp"
    assert cfg.downstream.headers == {}


def test_config_downstream_http_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_DOWNSTREAM_URL", "http://localhost:3000/mcp")
    monkeypatch.setenv("AUDIT_DOWNSTREAM_HEADERS", '{"X-Api-Key": "tok123"}')
    cfg = load_config()
    assert isinstance(cfg.downstream, DownstreamHttp)
    assert cfg.downstream.headers == {"X-Api-Key": "tok123"}


def test_config_downstream_in_json_file(tmp_path: Path) -> None:
    data: dict[str, Any] = {
        "downstream": {"kind": "stdio", "command": "node", "args": ["server.js"]},
    }
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(data))
    cfg = load_config(p)
    assert isinstance(cfg.downstream, DownstreamStdio)
    assert cfg.downstream.command == "node"
    assert cfg.downstream.args == ["server.js"]


def test_config_retention_days_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "30")
    cfg = load_config()
    assert cfg.retention_days == 30


def test_config_retention_days_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "0")
    cfg = load_config()
    assert cfg.retention_days is None


def test_config_retention_days_default_none() -> None:
    cfg = load_config()
    assert cfg.retention_days is None


def test_config_retention_days_from_file(tmp_path: Path) -> None:
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"retention_days": 7}))
    cfg = load_config(p)
    assert cfg.retention_days == 7
