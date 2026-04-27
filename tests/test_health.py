"""Tests for the /healthz ASGI handler."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from mcp_audit_logger.__main__ import _make_health_handler
from mcp_audit_logger.storage import AuditStore


@pytest.fixture()
def store(tmp_path: Path) -> AuditStore:
    return AuditStore(tmp_path / "audit.db")


def _client(store: AuditStore, proxy: Any = None) -> TestClient:
    app = Starlette(routes=[Mount("/healthz", app=_make_health_handler(store, proxy))])
    return TestClient(app, raise_server_exceptions=True)


def test_healthz_returns_200_when_no_downstream(store: AuditStore) -> None:
    resp = _client(store).get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_writable"] is True
    assert body["downstream"] == "not_configured"
    assert "version" in body


def test_healthz_returns_200_when_downstream_connected(store: AuditStore) -> None:
    class _ConnectedProxy:
        _session = object()  # truthy

    resp = _client(store, proxy=_ConnectedProxy()).get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["downstream"] == "connected"


def test_healthz_returns_503_when_downstream_disconnected(store: AuditStore) -> None:
    class _DisconnectedProxy:
        _session = None

    resp = _client(store, proxy=_DisconnectedProxy()).get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["downstream"] == "disconnected"


def test_healthz_rejects_post(store: AuditStore) -> None:
    assert _client(store).post("/healthz").status_code == 405


def test_healthz_returns_json_content_type(store: AuditStore) -> None:
    resp = _client(store).get("/healthz")
    assert resp.headers["content-type"] == "application/json"
    json.loads(resp.text)  # parses cleanly
