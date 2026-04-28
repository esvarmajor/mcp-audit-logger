"""obs_* tool definitions and dispatch.

Mirrors the AUDIT_TOOLS / _run_audit_tool pattern in server.py — a list of
types.Tool entries plus a single dispatch function the server calls.
The dispatch returns (payload_dict, is_error) so the server can wrap it
in CallToolResult with the right `isError` flag.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types

from . import correlation, errors, prometheus, schemas, traces
from .alerts import AlertManagerClient
from .prometheus import PrometheusClient
from .traces import TraceBackend

# ---------------------------------------------------------------- tool defs

OBS_TOOLS: list[types.Tool] = [
    types.Tool(
        name="obs_query_metric",
        description=(
            "Run a raw PromQL range query against Prometheus. "
            "Escape hatch for queries not covered by higher-level tools."
        ),
        inputSchema=schemas.QueryMetricArgs.model_json_schema(),
    ),
    types.Tool(
        name="obs_get_service_metrics",
        description=(
            "Return RED metrics (error rate, request rate, p50/p95/p99 latency) "
            "for a service over a window. Metric names are configurable; defaults "
            "follow OpenTelemetry HTTP semantic conventions."
        ),
        inputSchema=schemas.GetServiceMetricsArgs.model_json_schema(),
    ),
    types.Tool(
        name="obs_list_instrumented_services",
        description=(
            "List all services currently reporting the configured request-count metric. "
            "Useful for discovery before targeting other tools."
        ),
        inputSchema=schemas.ListInstrumentedServicesArgs.model_json_schema(),
    ),
    types.Tool(
        name="obs_get_trace",
        description=(
            "Fetch a complete distributed trace by trace_id and return spans as a "
            "recursive parent-child tree (each node has duration_ms, status, tags, children). "
            "Trace-not-found returns a structured error rather than an empty result."
        ),
        inputSchema=schemas.GetTraceArgs.model_json_schema(),
    ),
    types.Tool(
        name="obs_find_traces",
        description=(
            "Search distributed traces by service and time range, with optional "
            "min_duration_ms and error_only filters. Returns trace metadata "
            "(trace_id, root_name, duration_ms, error) for triage without fetching full traces."
        ),
        inputSchema=schemas.FindTracesArgs.model_json_schema(),
    ),
    types.Tool(
        name="obs_get_slow_spans",
        description=(
            "Find spans exceeding a latency threshold over a window, grouped by operation, "
            "with p99/p50 and sample trace IDs per operation."
        ),
        inputSchema=schemas.GetSlowSpansArgs.model_json_schema(),
    ),
    types.Tool(
        name="obs_investigate",
        description=(
            "Composite incident-triage tool. Concurrently pulls metrics deltas (current vs "
            "prior equivalent window), errored and slowest traces, recent log lines, and "
            "active/recently-resolved alerts for a service. Returns a structured object "
            "with a 1-3 sentence English `summary` derived deterministically from the data. "
            "Partial results returned if any backend is unreachable; failures appear in `errors`."
        ),
        inputSchema=schemas.InvestigateArgs.model_json_schema(),
    ),
]

OBS_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in OBS_TOOLS)


# ---------------------------------------------------------------- dispatch


async def run_obs_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    prometheus_client: PrometheusClient | None,
    trace_backend: TraceBackend | None,
    alerts_client: AlertManagerClient | None,
    store: Any | None,
    metric_names: dict[str, str],
) -> tuple[list[types.ContentBlock], bool]:
    """Dispatch an obs_* tool. Returns (content blocks, is_error)."""
    args = arguments or {}
    log_lookup = _make_log_lookup(store) if store is not None else None

    if name == "obs_query_metric":
        result = await prometheus.run_query_metric(prometheus_client, args)
    elif name == "obs_get_service_metrics":
        result = await prometheus.run_get_service_metrics(
            prometheus_client, args, metric_names
        )
    elif name == "obs_list_instrumented_services":
        result = await prometheus.run_list_instrumented_services(
            prometheus_client, args, metric_names
        )
    elif name == "obs_get_trace":
        result = await traces.run_get_trace(trace_backend, args, log_lookup=log_lookup)
    elif name == "obs_find_traces":
        result = await traces.run_find_traces(trace_backend, args, log_lookup=log_lookup)
    elif name == "obs_get_slow_spans":
        result = await traces.run_get_slow_spans(trace_backend, args)
    elif name == "obs_investigate":
        try:
            inv = schemas.InvestigateArgs.model_validate(args)
        except ValueError as e:
            result = errors.invalid_arguments(str(e))
        else:
            result = await correlation.investigate(
                service=inv.service,
                start_iso=inv.start,
                end_iso=inv.end,
                prom=prometheus_client,
                trace=trace_backend,
                alerts=alerts_client,
                store=store,
                metric_names=metric_names,
            )
    else:
        result = {"error": "unknown_tool", "name": name}

    is_err = errors.is_error(result)
    text = json.dumps(result, indent=2, default=str)
    return [types.TextContent(type="text", text=text)], is_err


def _make_log_lookup(store: Any):
    """Build a closure that searches the AuditStore for trace_id substring matches.

    Returns None-safe — if the store doesn't have search_arguments, the
    closure raises AttributeError (caught upstream as "no logs available").
    """

    def lookup(trace_id: str) -> list[dict[str, Any]]:
        if not trace_id:
            return []
        # Match the trace_id anywhere in the serialised arguments JSON.
        return store.search_arguments(pattern=f"%{trace_id}%", limit=20)

    return lookup
