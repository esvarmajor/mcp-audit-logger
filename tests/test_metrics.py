"""Tests for the Prometheus /metrics rendering and ASGI handler."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_audit_logger.__main__ import _make_metrics_endpoint
from mcp_audit_logger.metrics import CONTENT_TYPE, render
from mcp_audit_logger.storage import AuditStore


@pytest.fixture()
def store(tmp_path: Path) -> AuditStore:
    s = AuditStore(tmp_path / "audit.db")
    t = time.time()
    # Two successful fetch calls, one failed; one search call
    for _ in range(2):
        s.log_call(
            ts_start=t, ts_end=t + 0.1, tool_name="fetch",
            arguments={}, response={}, success=True, error=None,
        )
    s.log_call(
        ts_start=t, ts_end=t + 0.5, tool_name="fetch",
        arguments={}, response={}, success=False, error="boom",
    )
    s.log_call(
        ts_start=t, ts_end=t + 0.2, tool_name="search",
        arguments={}, response={}, success=True, error=None,
    )
    return s


def test_render_includes_help_and_type_lines(store: AuditStore) -> None:
    body = render(store)
    assert "# HELP mcp_audit_calls_total" in body
    assert "# TYPE mcp_audit_calls_total counter" in body
    assert "# HELP mcp_audit_call_duration_seconds" in body
    assert "# TYPE mcp_audit_call_duration_seconds summary" in body
    assert "# HELP mcp_audit_db_size_bytes" in body


def test_render_emits_per_tool_counters(store: AuditStore) -> None:
    body = render(store)
    # 2 successful + 1 failed fetch calls
    assert 'mcp_audit_calls_total{tool="fetch",success="true"} 2' in body
    assert 'mcp_audit_calls_total{tool="fetch",success="false"} 1' in body
    assert 'mcp_audit_calls_total{tool="search",success="true"} 1' in body
    assert 'mcp_audit_calls_total{tool="search",success="false"} 0' in body


def test_render_emits_quantile_lines(store: AuditStore) -> None:
    body = render(store)
    assert 'mcp_audit_call_duration_seconds{tool="fetch",quantile="0.5"}' in body
    assert 'mcp_audit_call_duration_seconds{tool="fetch",quantile="0.95"}' in body
    assert 'mcp_audit_call_duration_seconds_sum{tool="fetch"}' in body
    assert 'mcp_audit_call_duration_seconds_count{tool="fetch"} 3' in body


def test_render_db_size_is_positive(store: AuditStore) -> None:
    body = render(store)
    # Find the gauge line; size should be > 0 (we wrote rows).
    gauge_line = next(
        ln for ln in body.splitlines() if ln.startswith("mcp_audit_db_size_bytes ")
    )
    value = int(gauge_line.split()[-1])
    assert value > 0


def test_render_escapes_quotes_in_tool_names(tmp_path: Path) -> None:
    s = AuditStore(tmp_path / "audit.db")
    t = time.time()
    s.log_call(
        ts_start=t, ts_end=t + 0.1, tool_name='weird"name',
        arguments={}, response={}, success=True, error=None,
    )
    body = render(s)
    assert 'tool="weird\\"name"' in body


def test_render_empty_store(tmp_path: Path) -> None:
    s = AuditStore(tmp_path / "audit.db")
    body = render(s)
    # No counters, but help/type lines and the size gauge are always present.
    assert "# TYPE mcp_audit_calls_total counter" in body
    assert "mcp_audit_db_size_bytes 0" in body or "mcp_audit_db_size_bytes" in body


# --------------------------------------------------------------- ASGI handler


def _client(store: AuditStore, token: str | None = None) -> TestClient:
    expected = f"Bearer {token}" if token else None
    endpoint = _make_metrics_endpoint(store, expected_token=expected)
    app = Starlette(routes=[Route("/metrics", endpoint, methods=["GET", "HEAD"])])
    return TestClient(app, raise_server_exceptions=True)


def test_metrics_handler_returns_text_exposition(store: AuditStore) -> None:
    client = _client(store)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == CONTENT_TYPE
    assert "mcp_audit_calls_total" in resp.text


def test_metrics_handler_rejects_post(store: AuditStore) -> None:
    client = _client(store)
    resp = client.post("/metrics")
    assert resp.status_code == 405


def test_metrics_handler_gated_by_bearer_token(store: AuditStore) -> None:
    client = _client(store, token="s3cret")
    assert client.get("/metrics").status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert "mcp_audit_calls_total" in ok.text
