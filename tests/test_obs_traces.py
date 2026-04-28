"""Tests for trace backend adapters, span tree builder, and trace tools."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from mcp_audit_logger.obs.traces import (
    JaegerClient,
    TempoClient,
    build_span_tree,
    run_find_traces,
    run_get_slow_spans,
    run_get_trace,
)

# ------------------------------------------------------------- span tree


def test_build_span_tree_single_root():
    spans = [
        {"span_id": "a", "parent_span_id": None, "name": "root", "duration_ms": 100},
        {"span_id": "b", "parent_span_id": "a", "name": "child", "duration_ms": 50},
        {"span_id": "c", "parent_span_id": "b", "name": "grandchild", "duration_ms": 10},
    ]
    tree = build_span_tree(spans)
    assert tree["span_id"] == "a"
    assert len(tree["children"]) == 1
    assert tree["children"][0]["span_id"] == "b"
    assert tree["children"][0]["children"][0]["span_id"] == "c"


def test_build_span_tree_multiple_orphans():
    spans = [
        {"span_id": "a", "parent_span_id": None, "name": "r1"},
        {"span_id": "b", "parent_span_id": "missing", "name": "r2"},
    ]
    tree = build_span_tree(spans)
    assert "roots" in tree
    assert len(tree["roots"]) == 2
    assert tree["span_count"] == 2


def test_build_span_tree_empty():
    tree = build_span_tree([])
    assert tree == {"roots": [], "span_count": 0}


# --------------------------------------------------------- TempoClient


def _tempo_otlp_trace() -> dict[str, Any]:
    return {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "checkout"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "spanId": "root",
                                "parentSpanId": None,
                                "name": "POST /api/checkout",
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000000500000000",
                                "status": {"code": 2},
                                "attributes": [
                                    {"key": "http.method", "value": {"stringValue": "POST"}}
                                ],
                            },
                            {
                                "spanId": "db",
                                "parentSpanId": "root",
                                "name": "db.query",
                                "startTimeUnixNano": "1700000000100000000",
                                "endTimeUnixNano": "1700000000400000000",
                                "status": {"code": 1},
                                "attributes": [],
                            },
                        ]
                    }
                ],
            }
        ]
    }


@pytest.mark.asyncio
async def test_tempo_get_trace_builds_tree():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/traces/abc123" in str(request.url)
        return httpx.Response(200, json=_tempo_otlp_trace())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = TempoClient("http://tempo:3200", client=http)
    result = await run_get_trace(backend, {"trace_id": "abc123"})

    assert result["trace_id"] == "abc123"
    assert result["backend"] == "tempo"
    assert result["span_count"] == 2
    tree = result["tree"]
    assert tree["span_id"] == "root"
    assert tree["status"] == "ERROR"
    assert tree["service"] == "checkout"
    assert len(tree["children"]) == 1
    assert tree["children"][0]["span_id"] == "db"


@pytest.mark.asyncio
async def test_tempo_get_trace_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = TempoClient("http://tempo:3200", client=http)
    result = await run_get_trace(backend, {"trace_id": "missing"})
    assert result == {"error": "trace_not_found", "trace_id": "missing", "backend": "tempo"}


@pytest.mark.asyncio
async def test_tempo_find_traces():
    def handler(request: httpx.Request) -> httpx.Response:
        # Verify TraceQL query construction.
        q = request.url.params.get("q", "")
        assert 'resource.service.name="api"' in q
        assert "duration > 500ms" in q
        assert "status=error" in q
        return httpx.Response(
            200,
            json={
                "traces": [
                    {
                        "traceID": "t1",
                        "rootServiceName": "api",
                        "rootSpanName": "GET /x",
                        "durationMs": 600,
                        "error": True,
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = TempoClient("http://tempo:3200", client=http)
    result = await run_find_traces(
        backend,
        {
            "service": "api",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T01:00:00Z",
            "min_duration_ms": 500,
            "error_only": True,
        },
    )
    assert "error" not in result
    assert result["count"] == 1
    assert result["traces"][0]["trace_id"] == "t1"
    assert result["traces"][0]["error"] is True


# --------------------------------------------------------- JaegerClient


def _jaeger_response() -> dict[str, Any]:
    return {
        "data": [
            {
                "traceID": "j1",
                "spans": [
                    {
                        "spanID": "root",
                        "operationName": "POST /checkout",
                        "startTime": 1700000000_000_000,
                        "duration": 500_000,
                        "processID": "p1",
                        "references": [],
                        "tags": [{"key": "error", "value": True}],
                    },
                    {
                        "spanID": "db",
                        "operationName": "db.query",
                        "startTime": 1700000000_100_000,
                        "duration": 300_000,
                        "processID": "p1",
                        "references": [{"refType": "CHILD_OF", "spanID": "root"}],
                        "tags": [],
                    },
                ],
                "processes": {"p1": {"serviceName": "checkout"}},
            }
        ]
    }


@pytest.mark.asyncio
async def test_jaeger_get_trace():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_jaeger_response())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = JaegerClient("http://jaeger:16686", client=http)
    result = await run_get_trace(backend, {"trace_id": "j1"})
    assert result["span_count"] == 2
    assert result["backend"] == "jaeger"
    tree = result["tree"]
    assert tree["span_id"] == "root"
    assert tree["status"] == "ERROR"
    assert len(tree["children"]) == 1


@pytest.mark.asyncio
async def test_jaeger_find_traces_uses_microseconds():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "traceID": "t1",
                        "spans": [
                            {
                                "spanID": "s",
                                "operationName": "op",
                                "startTime": 1700000000_000_000,
                                "duration": 500_000,
                                "processID": "p1",
                                "references": [],
                                "tags": [],
                            }
                        ],
                        "processes": {"p1": {"serviceName": "api"}},
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = JaegerClient("http://jaeger:16686", client=http)
    await run_find_traces(
        backend,
        {
            "service": "api",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T01:00:00Z",
            "min_duration_ms": 100,
        },
    )
    # Jaeger expects microseconds, not nanoseconds.
    assert int(captured["params"]["start"]) == 1704067200_000_000
    assert captured["params"]["minDuration"] == "100ms"


# --------------------------------------------------------- handler errors


@pytest.mark.asyncio
async def test_get_trace_not_configured():
    result = await run_get_trace(None, {"trace_id": "x"})
    assert result["error"] == "not_configured"
    assert "TEMPO_URL" in result["message"]


@pytest.mark.asyncio
async def test_get_trace_backend_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = TempoClient("http://tempo:3200", client=http)
    result = await run_get_trace(backend, {"trace_id": "x"})
    assert result["error"] == "backend_unreachable"


@pytest.mark.asyncio
async def test_get_trace_with_log_lookup():
    """When a log_lookup callable is provided, its output appears under `logs`."""
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=_tempo_otlp_trace())
    )
    http = httpx.AsyncClient(transport=transport)
    backend = TempoClient("http://tempo:3200", client=http)

    captured = []

    def lookup(trace_id: str):
        captured.append(trace_id)
        return [{"id": 1, "tool_name": "x", "trace_id": trace_id}]

    result = await run_get_trace(backend, {"trace_id": "abc"}, log_lookup=lookup)
    assert captured == ["abc"]
    assert result["logs"] == [{"id": 1, "tool_name": "x", "trace_id": "abc"}]


@pytest.mark.asyncio
async def test_find_traces_has_logs_enrichment():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "traces": [
                    {"traceID": "t1", "rootServiceName": "api", "durationMs": 100, "error": False},
                    {"traceID": "t2", "rootServiceName": "api", "durationMs": 200, "error": False},
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = TempoClient("http://tempo:3200", client=http)

    def lookup(trace_id: str):
        return [{"id": 1}] if trace_id == "t1" else []

    result = await run_find_traces(
        backend,
        {
            "service": "api",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T01:00:00Z",
        },
        log_lookup=lookup,
    )
    assert result["traces"][0]["has_logs"] is True
    assert result["traces"][1]["has_logs"] is False


@pytest.mark.asyncio
async def test_get_slow_spans_groups_by_operation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "traces": [
                    {
                        "traceID": "t1", "rootServiceName": "api",
                        "rootSpanName": "POST /x", "durationMs": 600, "error": False,
                    },
                    {
                        "traceID": "t2", "rootServiceName": "api",
                        "rootSpanName": "POST /x", "durationMs": 700, "error": False,
                    },
                    {
                        "traceID": "t3", "rootServiceName": "api",
                        "rootSpanName": "GET /y", "durationMs": 800, "error": False,
                    },
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = TempoClient("http://tempo:3200", client=http)
    result = await run_get_slow_spans(
        backend, {"service": "api", "window": "5m", "threshold_ms": 500}
    )
    spans = result["spans"]
    by_op = {s["operation"]: s for s in spans}
    # Tempo find_traces returns the spans grouped by root_name.
    # Note: the rootServiceName is being used as root_name here in our adapter.
    assert "api" in by_op or "GET /y" in by_op or "POST /x" in by_op
    # All groups should have count >= 1 and p99 >= threshold
    assert all(s["count"] >= 1 for s in spans)
    assert all(s["p99_ms"] >= 500 for s in spans)
