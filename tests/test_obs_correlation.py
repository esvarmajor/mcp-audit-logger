"""Tests for the obs_investigate correlation tool.

These exercise the fan-out concurrency, partial-result behavior, and the
deterministic summary heuristics. Backends are stubbed with httpx.MockTransport
plus per-test artificial delays.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from mcp_audit_logger.obs.alerts import AlertManagerClient
from mcp_audit_logger.obs.correlation import build_summary, investigate
from mcp_audit_logger.obs.prometheus import PrometheusClient
from mcp_audit_logger.obs.traces import TempoClient
from mcp_audit_logger.storage import AuditStore

# ---------------------------------------------------- helpers


def _instant(value: float) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, str(value)]}]},
    }


def _build_prom_client(
    value_map: dict[str, float] | None = None, *, delay: float = 0.0
) -> PrometheusClient:
    """Return a Prom client whose responses are determined by query substring."""
    value_map = value_map or {}

    async def _async_handler(request: httpx.Request) -> httpx.Response:
        if delay:
            await asyncio.sleep(delay)
        q = request.url.params.get("query", "")
        for substr, val in value_map.items():
            if substr in q:
                return httpx.Response(200, json=_instant(val))
        return httpx.Response(200, json=_instant(0.0))

    transport = httpx.MockTransport(_async_handler)
    return PrometheusClient(
        "http://prom:9090", client=httpx.AsyncClient(transport=transport)
    )


def _build_tempo_client(traces: list[dict[str, Any]], *, delay: float = 0.0) -> TempoClient:
    async def _async_handler(request: httpx.Request) -> httpx.Response:
        if delay:
            await asyncio.sleep(delay)
        return httpx.Response(200, json={"traces": traces})

    transport = httpx.MockTransport(_async_handler)
    return TempoClient("http://tempo:3200", client=httpx.AsyncClient(transport=transport))


def _build_alerts_client(active: list[dict[str, Any]], *, delay: float = 0.0) -> AlertManagerClient:
    async def _async_handler(request: httpx.Request) -> httpx.Response:
        if delay:
            await asyncio.sleep(delay)
        if request.url.params.get("active") == "true":
            return httpx.Response(200, json=active)
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(_async_handler)
    return AlertManagerClient("http://am:9093", client=httpx.AsyncClient(transport=transport))


# ----------------------------------------------- happy path & shape


@pytest.mark.asyncio
async def test_investigate_happy_path(tmp_path: Path):
    store = AuditStore(tmp_path / "audit.db")
    prom = _build_prom_client(
        {
            "histogram_quantile(0.99": 800.0,
            'status_code=~"5..': 0.04,
            'sum(rate(http_server_requests_total': 45.0,
        }
    )
    tempo = _build_tempo_client(
        [
            {
                "traceID": "t1", "rootServiceName": "api",
                "rootSpanName": "POST /x", "durationMs": 1200, "error": True,
            },
            {
                "traceID": "t2", "rootServiceName": "api",
                "rootSpanName": "POST /x", "durationMs": 800, "error": True,
            },
        ]
    )
    alerts = _build_alerts_client(
        [
            {
                "labels": {"alertname": "HighErrorRate", "service": "api"},
                "endsAt": "2099-01-01T00:00:00Z",
            }
        ]
    )

    out = await investigate(
        service="api",
        start_iso="2024-01-01T00:00:00Z",
        end_iso="2024-01-01T01:00:00Z",
        prom=prom,
        trace=tempo,
        alerts=alerts,
        store=store,
        metric_names={},
    )
    assert out["service"] == "api"
    assert out["window"]["start"] == "2024-01-01T00:00:00Z"
    assert out["metrics_delta"] is not None
    assert out["anomalous_traces"] is not None
    assert isinstance(out["log_sample"], list)
    assert out["alerts"] is not None
    assert out["errors"] == []
    assert out["summary"]
    # Summary should mention the active alert.
    assert "HighErrorRate" in out["summary"]


# ----------------------------------------------- partial results


@pytest.mark.asyncio
async def test_investigate_partial_when_one_backend_fails(tmp_path: Path):
    """If Tempo errors, anomalous_traces is None but the rest still arrives."""
    store = AuditStore(tmp_path / "audit.db")
    prom = _build_prom_client({"http_server_requests_total": 45.0})

    async def _broken_tempo(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    tempo = TempoClient(
        "http://tempo:3200",
        client=httpx.AsyncClient(transport=httpx.MockTransport(_broken_tempo)),
    )
    alerts = _build_alerts_client([])

    out = await investigate(
        service="api",
        start_iso="2024-01-01T00:00:00Z",
        end_iso="2024-01-01T01:00:00Z",
        prom=prom,
        trace=tempo,
        alerts=alerts,
        store=store,
        metric_names={},
    )
    assert out["anomalous_traces"] is None
    assert out["metrics_delta"] is not None
    assert out["alerts"] is not None
    # Tempo failure must show up in errors.
    trace_errors = [e for e in out["errors"] if e["step"] == "traces"]
    assert len(trace_errors) == 1
    assert trace_errors[0]["backend"] == "tempo"
    # Summary must still be generated.
    assert out["summary"]


@pytest.mark.asyncio
async def test_investigate_with_no_clients_configured(tmp_path: Path):
    """All backends None — investigate should still return, with errors populated."""
    store = AuditStore(tmp_path / "audit.db")
    out = await investigate(
        service="api",
        start_iso="2024-01-01T00:00:00Z",
        end_iso="2024-01-01T01:00:00Z",
        prom=None,
        trace=None,
        alerts=None,
        store=store,
        metric_names={},
    )
    steps = {e["step"] for e in out["errors"]}
    assert {"metrics", "traces", "alerts"}.issubset(steps)
    # Summary should still be generated, mentioning partial data.
    assert "Partial" in out["summary"] or "No anomalies" in out["summary"]


# ----------------------------------------------- concurrency


@pytest.mark.asyncio
async def test_investigate_runs_concurrently(tmp_path: Path):
    """Total wall-clock time must be ~max(delays), not sum(delays)."""
    store = AuditStore(tmp_path / "audit.db")
    delay = 0.15  # 150ms each
    prom = _build_prom_client({"http_server_requests_total": 45.0}, delay=delay)
    tempo = _build_tempo_client([], delay=delay)
    alerts = _build_alerts_client([], delay=delay)

    t0 = time.perf_counter()
    await investigate(
        service="api",
        start_iso="2024-01-01T00:00:00Z",
        end_iso="2024-01-01T01:00:00Z",
        prom=prom,
        trace=tempo,
        alerts=alerts,
        store=store,
        metric_names={},
    )
    elapsed = time.perf_counter() - t0
    # Sequential would be: 6 prom queries (current+prior * 3 metrics) at 150ms each = 900ms,
    # plus tempo 2 calls = 300ms, plus alerts 2 calls = 300ms. Sum >> 1500ms.
    # Concurrent fan-out: prom is the longest path (~900ms) since the metrics deltas
    # compute serially within the metrics step. Other steps run alongside.
    # We assert simply: less than the sum of all delays.
    sequential_minimum = 6 * delay + 2 * delay + 2 * delay  # = 1.5s
    assert elapsed < sequential_minimum, (
        f"investigate took {elapsed:.2f}s; sequential would be ~{sequential_minimum:.2f}s. "
        f"Backends should fan out via task group."
    )


# ----------------------------------------------- summary heuristics


def _stub_out(**parts: Any) -> dict[str, Any]:
    base = {
        "metrics_delta": None,
        "anomalous_traces": None,
        "log_sample": None,
        "alerts": None,
        "errors": [],
    }
    base.update(parts)
    return base


def test_summary_error_rate_spike():
    out = _stub_out(
        metrics_delta={
            "error_rate": {"current": 0.12, "prior": 0.03, "delta_pct": 300.0},
            "request_rate_rps": {"current": 45, "prior": 45, "delta_pct": 0},
            "latency_p99_ms": {"current": 800, "prior": 800, "delta_pct": 0},
        }
    )
    summary = build_summary(out, "api")
    assert "spiked" in summary.lower()


def test_summary_dominant_operation():
    out = _stub_out(
        anomalous_traces={
            "errored": [
                {"trace_id": "t1", "root_name": "POST /checkout"},
                {"trace_id": "t2", "root_name": "POST /checkout"},
                {"trace_id": "t3", "root_name": "POST /checkout"},
                {"trace_id": "t4", "root_name": "GET /unrelated"},
            ],
            "slowest": [],
        }
    )
    summary = build_summary(out, "api")
    assert "POST /checkout" in summary


def test_summary_log_keyword():
    out = _stub_out(
        log_sample=[
            {"error": "downstream timeout", "response": {}},
            {"error": "got 503 from payments-service", "response": {}},
        ]
    )
    summary = build_summary(out, "api")
    assert "timeout" in summary.lower() or "503" in summary


def test_summary_active_alerts():
    out = _stub_out(
        alerts={
            "active": [{"labels": {"alertname": "ServiceDown"}}],
            "recently_resolved": [],
        }
    )
    summary = build_summary(out, "api")
    assert "ServiceDown" in summary


def test_summary_no_anomalies():
    out = _stub_out()
    summary = build_summary(out, "api")
    assert "No anomalies" in summary


def test_summary_partial_data():
    out = _stub_out(errors=[{"step": "metrics", "backend": "prometheus", "message": "x"}])
    summary = build_summary(out, "api")
    assert "Partial" in summary or "metrics" in summary


# ----------------------------------------------- arg validation


@pytest.mark.asyncio
async def test_investigate_invalid_iso():
    out = await investigate(
        service="api",
        start_iso="not-a-date",
        end_iso="2024-01-01T01:00:00Z",
        prom=None,
        trace=None,
        alerts=None,
        store=None,
        metric_names={},
    )
    assert out["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_investigate_end_before_start():
    out = await investigate(
        service="api",
        start_iso="2024-01-01T01:00:00Z",
        end_iso="2024-01-01T00:00:00Z",
        prom=None,
        trace=None,
        alerts=None,
        store=None,
        metric_names={},
    )
    assert out["error"] == "invalid_arguments"
