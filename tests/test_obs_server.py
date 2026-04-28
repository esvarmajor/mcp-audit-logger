"""Integration tests for obs_* tools through the full MCP round-trip.

Builds a server with mocked obs clients and calls tools via the in-process
client session helper (same fixture pattern as tests/test_server.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import mcp.types as types
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_audit_logger.obs.alerts import AlertManagerClient
from mcp_audit_logger.obs.prometheus import PrometheusClient
from mcp_audit_logger.obs.traces import TempoClient
from mcp_audit_logger.server import OBS_TOOL_NAMES, build_server
from mcp_audit_logger.storage import AuditStore


def _instant(value: float) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, str(value)]}]},
    }


@pytest.fixture()
def store(tmp_path: Path) -> AuditStore:
    return AuditStore(tmp_path / "audit.db")


@pytest.fixture()
def prom_client() -> PrometheusClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_instant(0.42))

    return PrometheusClient(
        "http://prom:9090", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


@pytest.fixture()
def tempo_client() -> TempoClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/traces/" in str(request.url):
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"traces": []})

    return TempoClient(
        "http://tempo:3200", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


@pytest.fixture()
def alerts_client() -> AlertManagerClient:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    return AlertManagerClient(
        "http://am:9093",
        client=httpx.AsyncClient(transport=transport),
    )


async def _call(server, name: str, args: dict[str, Any]) -> types.CallToolResult:
    async with create_connected_server_and_client_session(server) as client:
        return await client.call_tool(name, args)


@pytest.mark.asyncio
async def test_obs_tools_listed(store: AuditStore) -> None:
    srv = build_server(proxy=None, store=store)
    async with create_connected_server_and_client_session(srv) as client:
        result = await client.list_tools()
    names = {t.name for t in result.tools}
    assert OBS_TOOL_NAMES.issubset(names), f"missing: {OBS_TOOL_NAMES - names}"


@pytest.mark.asyncio
async def test_obs_query_metric_through_mcp(
    store: AuditStore, prom_client: PrometheusClient
) -> None:
    srv = build_server(proxy=None, store=store, prometheus_client=prom_client)
    result = await _call(
        srv,
        "obs_query_metric",
        {
            "expr": "up",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T01:00:00Z",
            "step": "30s",
        },
    )
    assert result.isError is not True
    data = json.loads(result.content[0].text)
    assert data["expr"] == "up"


@pytest.mark.asyncio
async def test_obs_query_metric_not_configured_sets_is_error(store: AuditStore) -> None:
    """No prometheus client wired → tool returns structured error AND isError=True."""
    srv = build_server(proxy=None, store=store)
    result = await _call(
        srv,
        "obs_query_metric",
        {
            "expr": "up",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T01:00:00Z",
            "step": "30s",
        },
    )
    assert result.isError is True
    data = json.loads(result.content[0].text)
    assert data["error"] == "not_configured"
    assert data["backend"] == "prometheus"


@pytest.mark.asyncio
async def test_obs_get_trace_not_found_through_mcp(
    store: AuditStore, tempo_client: TempoClient
) -> None:
    srv = build_server(proxy=None, store=store, trace_backend=tempo_client)
    result = await _call(srv, "obs_get_trace", {"trace_id": "missing"})
    assert result.isError is True
    data = json.loads(result.content[0].text)
    assert data["error"] == "trace_not_found"


@pytest.mark.asyncio
async def test_obs_investigate_through_mcp(
    store: AuditStore,
    prom_client: PrometheusClient,
    tempo_client: TempoClient,
    alerts_client: AlertManagerClient,
) -> None:
    srv = build_server(
        proxy=None,
        store=store,
        prometheus_client=prom_client,
        trace_backend=tempo_client,
        alerts_client=alerts_client,
    )
    result = await _call(
        srv,
        "obs_investigate",
        {
            "service": "api",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T01:00:00Z",
        },
    )
    assert result.isError is not True
    data = json.loads(result.content[0].text)
    assert data["service"] == "api"
    assert "summary" in data
    assert "errors" in data
