"""Tests for the Prometheus client and metrics tools.

Uses httpx.MockTransport to stub HTTP responses — no network I/O.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from mcp_audit_logger.obs.prometheus import (
    DEFAULT_METRIC_NAMES,
    PrometheusClient,
    _extract_scalar,
    resolve_metric_names,
    run_get_service_metrics,
    run_list_instrumented_services,
    run_query_metric,
)

# ----------------------------------------------------------- mock-client helpers


def _instant_response(value: float) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1700000000, str(value)]}],
        },
    }


def _empty_response() -> dict[str, Any]:
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


def _make_client(handler) -> PrometheusClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return PrometheusClient("http://prometheus:9090", client=http)


# ------------------------------------------------------------------ unit tests


def test_resolve_metric_names_uses_defaults():
    out = resolve_metric_names(None)
    assert out == DEFAULT_METRIC_NAMES


def test_resolve_metric_names_overrides_partial():
    out = resolve_metric_names({"service_label": "service"})
    assert out["service_label"] == "service"
    # Other keys retain defaults.
    assert out["request_count"] == DEFAULT_METRIC_NAMES["request_count"]


def test_extract_scalar_from_vector():
    assert _extract_scalar(_instant_response(0.42)) == 0.42


def test_extract_scalar_empty():
    assert _extract_scalar(_empty_response()) is None


def test_extract_scalar_nan():
    nan_resp = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "NaN"]}]},
    }
    assert _extract_scalar(nan_resp) is None


# ------------------------------------------------------------- handler tests


@pytest.mark.asyncio
async def test_query_metric_success():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [{"metric": {"job": "api"}, "values": [[1700000000, "1.0"]]}],
                },
            },
        )

    client = _make_client(handler)
    result = await run_query_metric(
        client,
        {
            "expr": "up",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T01:00:00Z",
            "step": "30s",
        },
    )
    assert "error" not in result
    assert result["expr"] == "up"
    assert "/api/v1/query_range" in captured["url"]
    assert "data" in result["data"]


@pytest.mark.asyncio
async def test_query_metric_invalid_iso():
    client = _make_client(lambda req: httpx.Response(200, json=_empty_response()))
    result = await run_query_metric(
        client,
        {"expr": "up", "start": "not-a-date", "end": "2024-01-01T01:00:00Z", "step": "30s"},
    )
    assert result["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_query_metric_not_configured_when_client_none():
    result = await run_query_metric(
        None,
        {
            "expr": "up",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T01:00:00Z",
            "step": "30s",
        },
    )
    assert result == {
        "error": "not_configured",
        "backend": "prometheus",
        "message": "Set PROMETHEUS_URL to enable prometheus queries",
    }


@pytest.mark.asyncio
async def test_query_metric_backend_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _make_client(handler)
    result = await run_query_metric(
        client,
        {
            "expr": "up",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T01:00:00Z",
            "step": "30s",
        },
    )
    assert result["error"] == "backend_unreachable"
    assert result["backend"] == "prometheus"


@pytest.mark.asyncio
async def test_query_metric_upstream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="bad")

    client = _make_client(handler)
    result = await run_query_metric(
        client,
        {
            "expr": "up",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T01:00:00Z",
            "step": "30s",
        },
    )
    assert result["error"] == "upstream_error"
    assert result["status"] == 500


@pytest.mark.asyncio
async def test_get_service_metrics_constructs_promql():
    """Verify the tool issues five separate instant queries with the right shape."""
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        queries.append(q)
        # Vary response so we can sanity-check assignment.
        if "histogram_quantile(0.99" in q:
            return httpx.Response(200, json=_instant_response(2400.0))
        if "histogram_quantile(0.95" in q:
            return httpx.Response(200, json=_instant_response(800.0))
        if "histogram_quantile(0.5" in q:
            return httpx.Response(200, json=_instant_response(120.0))
        if 'status_code=~"5..' in q:
            return httpx.Response(200, json=_instant_response(0.04))
        return httpx.Response(200, json=_instant_response(45.0))

    client = _make_client(handler)
    result = await run_get_service_metrics(
        client, {"service": "api", "window": "5m"}, {}
    )
    assert result["service"] == "api"
    assert result["error_rate"] == 0.04
    assert result["request_rate_rps"] == 45.0
    assert result["latency_ms"]["p99"] == 2400.0
    assert result["latency_ms"]["p95"] == 800.0
    assert result["latency_ms"]["p50"] == 120.0
    # All five queries must reference the service label.
    assert all('service_name="api"' in q for q in queries)
    # Window must propagate.
    assert all("[5m]" in q for q in queries)


@pytest.mark.asyncio
async def test_get_service_metrics_uses_metric_name_overrides():
    """Custom metric_names should flow through into PromQL exactly."""
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params.get("query", ""))
        return httpx.Response(200, json=_instant_response(1.0))

    client = _make_client(handler)
    overrides = {
        "request_count": "http_requests_total",
        "request_duration": "http_request_duration_seconds",
        "service_label": "service",
        "status_label": "status",
    }
    await run_get_service_metrics(client, {"service": "web", "window": "10m"}, overrides)
    assert any("http_requests_total" in q for q in queries)
    assert any("http_request_duration_seconds_bucket" in q for q in queries)
    assert all('service="web"' in q for q in queries)


@pytest.mark.asyncio
async def test_list_instrumented_services():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/v1/label/service_name/values" in str(request.url)
        return httpx.Response(
            200,
            json={"status": "success", "data": ["api", "web", "worker"]},
        )

    client = _make_client(handler)
    result = await run_list_instrumented_services(client, {}, {})
    assert result["services"] == ["api", "web", "worker"]
    assert result["label"] == "service_name"
    assert result["base_metric"] == "http_server_requests_total"


@pytest.mark.asyncio
async def test_list_instrumented_services_custom_base():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["match[]"] == "my_metric"
        return httpx.Response(200, json={"status": "success", "data": ["x"]})

    client = _make_client(handler)
    result = await run_list_instrumented_services(client, {"base_metric": "my_metric"}, {})
    assert result["base_metric"] == "my_metric"
