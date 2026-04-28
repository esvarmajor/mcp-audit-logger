"""Distributed-tracing client adapters and tool handlers.

Two backends behind a common Protocol:
    - TempoClient   — Grafana Tempo HTTP API
    - JaegerClient  — Jaeger Query API (REST v1; v3 endpoint reads similarly)

The selected backend is chosen at server-build time: if `cfg.tempo_url` is
non-empty, Tempo wins; otherwise Jaeger; otherwise None and the trace tools
return a structured `not_configured` error.

Span tree
---------
Both backends return a flat list of spans per trace. We assemble a recursive
{span_id, name, ..., children: [<recursive>]} tree before returning to callers.
Orphan spans (parent_span_id missing or unknown) are kept as additional roots
under a synthesized `roots` list when the trace has no single root.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

import httpx

from . import errors

# --------------------------------------------------------------------- protocol


@runtime_checkable
class TraceBackend(Protocol):
    backend_name: str

    async def get_trace(self, trace_id: str) -> list[dict[str, Any]] | None:
        """Return a flat list of spans, or None if trace not found."""

    async def find_traces(
        self,
        *,
        service: str,
        start: datetime,
        end: datetime,
        min_duration_ms: int | None,
        error_only: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return summary metadata for matching traces."""

    async def get_slow_spans(
        self, *, service: str, window: str, threshold_ms: int
    ) -> list[dict[str, Any]]:
        """Return spans grouped by operation, exceeding threshold."""


# ----------------------------------------------------------- helpers / parsing


def _parse_iso(s: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"Invalid ISO8601 in '{field}': {s!r} ({e})") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_window_to_seconds(window: str) -> int:
    """Parse Prom-style duration (e.g. '5m', '1h', '30s') to seconds."""
    if not window:
        raise ValueError("window cannot be empty")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    suffix = window[-1]
    if suffix not in units:
        raise ValueError(f"Unrecognized window unit: {window!r} (use s/m/h/d)")
    try:
        n = int(window[:-1])
    except ValueError as e:
        raise ValueError(f"Invalid window value: {window!r}") from e
    return n * units[suffix]


def build_span_tree(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert a flat list of spans (each with span_id and parent_span_id) into a tree.

    Returns:
        Either a single root node (with `children` recursively), or a
        synthetic envelope `{"roots": [...]}` if multiple roots exist.

    Each input span MUST have at minimum: span_id, name. parent_span_id may
    be None or missing — such spans are treated as roots.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for sp in spans:
        sid = sp.get("span_id")
        if not sid:
            continue
        node = {**sp, "children": []}
        by_id[sid] = node

    roots: list[dict[str, Any]] = []
    for sp in by_id.values():
        parent = sp.get("parent_span_id")
        if parent and parent in by_id:
            by_id[parent]["children"].append(sp)
        else:
            roots.append(sp)

    if len(roots) == 1:
        return roots[0]
    # Multi-root: orphans + actual root, or a fragmented trace. Keep all.
    return {"roots": roots, "span_count": len(by_id)}


# --------------------------------------------------------------------- Tempo


class TempoClient:
    """Grafana Tempo HTTP API client.

    Endpoints used:
      - GET /api/traces/{trace_id}        — single trace as OTLP-style JSON
      - GET /api/search                   — TraceQL search (pre-2.x: tag-based)

    Tempo's response format follows OTLP: batches → resource_spans → scope_spans → spans.
    We flatten this to our generic span dict.
    """

    backend_name = "tempo"

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._timeout = timeout
        self._client = client

    def _make_client(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(base_url=self._url, timeout=self._timeout)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        owns = self._client is None
        client = self._make_client()
        try:
            url = path if self._client is None else f"{self._url}{path}"
            return await client.get(url, params=params)
        finally:
            if owns:
                await client.aclose()

    async def get_trace(self, trace_id: str) -> list[dict[str, Any]] | None:
        resp = await self._get(f"/api/traces/{trace_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        body = resp.json()
        return _flatten_otlp_trace(body)

    async def find_traces(
        self,
        *,
        service: str,
        start: datetime,
        end: datetime,
        min_duration_ms: int | None,
        error_only: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        # TraceQL syntax (Tempo 2.x). Older Tempo accepts tag-style params.
        parts = [f'resource.service.name="{service}"']
        if min_duration_ms:
            parts.append(f"duration > {min_duration_ms}ms")
        if error_only:
            parts.append("status=error")
        traceql = "{" + " && ".join(parts) + "}"
        params: dict[str, Any] = {
            "q": traceql,
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "limit": limit,
        }
        resp = await self._get("/api/search", params)
        resp.raise_for_status()
        body = resp.json()
        out: list[dict[str, Any]] = []
        for t in body.get("traces", []) or []:
            out.append(
                {
                    "trace_id": t.get("traceID") or t.get("trace_id"),
                    "root_name": t.get("rootServiceName") or t.get("rootSpanName") or "",
                    "duration_ms": float(
                        t.get("durationMs") or (t.get("duration", 0) / 1_000_000) or 0
                    ),
                    "error": bool(t.get("error", False)),
                    "start_time": t.get("startTimeUnixNano"),
                }
            )
        return out

    async def get_slow_spans(
        self, *, service: str, window: str, threshold_ms: int
    ) -> list[dict[str, Any]]:
        # Compute window timeframe.
        secs = _parse_window_to_seconds(window)
        end = datetime.now(timezone.utc)
        start = end - _seconds_to_timedelta(secs)
        # No reliable cross-version Tempo aggregation API; fetch traces and group in-process.
        traces = await self.find_traces(
            service=service,
            start=start,
            end=end,
            min_duration_ms=threshold_ms,
            error_only=False,
            limit=200,
        )
        # Group by root_name (best we can do without fetching full traces per result).
        return _group_traces_by_operation(traces, threshold_ms)


def _flatten_otlp_trace(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten OTLP-style trace JSON into a list of span dicts."""
    spans: list[dict[str, Any]] = []
    batches = body.get("batches") or body.get("resource_spans") or body.get("resourceSpans") or []
    for batch in batches:
        resource = batch.get("resource", {})
        attrs = _otlp_attributes_to_dict(resource.get("attributes", []))
        service_name = attrs.get("service.name") or attrs.get("service_name") or ""
        scope_spans = (
            batch.get("scopeSpans")
            or batch.get("scope_spans")
            or batch.get("instrumentationLibrarySpans")
            or []
        )
        for ss in scope_spans:
            for sp in ss.get("spans", []) or []:
                spans.append(_otlp_span_to_generic(sp, service_name))
    return spans


def _otlp_span_to_generic(sp: dict[str, Any], service_name: str) -> dict[str, Any]:
    start_ns = int(sp.get("startTimeUnixNano") or sp.get("start_time_unix_nano") or 0)
    end_ns = int(sp.get("endTimeUnixNano") or sp.get("end_time_unix_nano") or 0)
    duration_ms = (end_ns - start_ns) / 1_000_000 if end_ns > start_ns else 0.0
    status = sp.get("status") or {}
    code = status.get("code", 0)
    # OTLP status: 0=Unset, 1=Ok, 2=Error
    status_str = "ERROR" if code == 2 else "OK"
    return {
        "span_id": sp.get("spanId") or sp.get("span_id") or "",
        "parent_span_id": sp.get("parentSpanId") or sp.get("parent_span_id") or None,
        "name": sp.get("name", ""),
        "service": service_name,
        "start_ns": start_ns,
        "duration_ms": duration_ms,
        "status": status_str,
        "tags": _otlp_attributes_to_dict(sp.get("attributes", [])),
    }


def _otlp_attributes_to_dict(attrs: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in attrs or []:
        key = a.get("key")
        if not key:
            continue
        val = a.get("value", {})
        # OTLP value is a oneOf: stringValue/intValue/boolValue/doubleValue/...
        if isinstance(val, dict):
            for k in (
                "stringValue", "string_value",
                "intValue", "int_value",
                "boolValue", "bool_value",
                "doubleValue", "double_value",
            ):
                if k in val:
                    out[key] = val[k]
                    break
            else:
                out[key] = val
        else:
            out[key] = val
    return out


def _seconds_to_timedelta(secs: int):
    from datetime import timedelta

    return timedelta(seconds=secs)


def _group_traces_by_operation(
    traces: list[dict[str, Any]], threshold_ms: int
) -> list[dict[str, Any]]:
    """Group traces by root_name; compute count, p99, and sample IDs."""
    by_op: dict[str, list[dict[str, Any]]] = {}
    for t in traces:
        if t.get("duration_ms", 0) < threshold_ms:
            continue
        by_op.setdefault(t.get("root_name") or "(unknown)", []).append(t)
    out: list[dict[str, Any]] = []
    for op, group in by_op.items():
        durations = sorted(float(t["duration_ms"]) for t in group)
        out.append(
            {
                "operation": op,
                "count": len(group),
                "p99_ms": _percentile(durations, 0.99),
                "p50_ms": _percentile(durations, 0.5),
                "sample_trace_ids": [t["trace_id"] for t in group[:3] if t.get("trace_id")],
            }
        )
    out.sort(key=lambda r: r["p99_ms"], reverse=True)
    return out


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, int(p * (len(sorted_values) - 1))))
    return sorted_values[idx]


# --------------------------------------------------------------------- Jaeger


class JaegerClient:
    """Jaeger Query API client (REST v1).

    Endpoints used:
      - GET /api/traces/{trace_id}      — single trace
      - GET /api/traces?service=...     — search
    Jaeger times are microseconds.
    """

    backend_name = "jaeger"

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._timeout = timeout
        self._client = client

    def _make_client(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(base_url=self._url, timeout=self._timeout)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        owns = self._client is None
        client = self._make_client()
        try:
            url = path if self._client is None else f"{self._url}{path}"
            return await client.get(url, params=params)
        finally:
            if owns:
                await client.aclose()

    async def get_trace(self, trace_id: str) -> list[dict[str, Any]] | None:
        resp = await self._get(f"/api/traces/{trace_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") or []
        if not data:
            return None
        return _flatten_jaeger_trace(data[0])

    async def find_traces(
        self,
        *,
        service: str,
        start: datetime,
        end: datetime,
        min_duration_ms: int | None,
        error_only: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "service": service,
            "start": int(start.timestamp() * 1_000_000),
            "end": int(end.timestamp() * 1_000_000),
            "limit": limit,
        }
        if min_duration_ms:
            params["minDuration"] = f"{min_duration_ms}ms"
        if error_only:
            params["tags"] = '{"error":"true"}'
        resp = await self._get("/api/traces", params)
        resp.raise_for_status()
        body = resp.json()
        out: list[dict[str, Any]] = []
        for trace in body.get("data") or []:
            spans = trace.get("spans") or []
            if not spans:
                continue
            root = _find_jaeger_root(spans)
            duration_us = max((s.get("duration", 0) for s in spans), default=0)
            has_error = any(_jaeger_span_has_error(s) for s in spans)
            out.append(
                {
                    "trace_id": trace.get("traceID") or "",
                    "root_name": (root.get("operationName") if root else "") or "",
                    "duration_ms": duration_us / 1000.0,
                    "error": has_error,
                    "start_time": root.get("startTime") if root else None,
                }
            )
        return out

    async def get_slow_spans(
        self, *, service: str, window: str, threshold_ms: int
    ) -> list[dict[str, Any]]:
        secs = _parse_window_to_seconds(window)
        end = datetime.now(timezone.utc)
        start = end - _seconds_to_timedelta(secs)
        traces = await self.find_traces(
            service=service,
            start=start,
            end=end,
            min_duration_ms=threshold_ms,
            error_only=False,
            limit=200,
        )
        return _group_traces_by_operation(traces, threshold_ms)


def _flatten_jaeger_trace(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a Jaeger trace's span list into our generic span dicts."""
    processes = trace.get("processes") or {}
    out: list[dict[str, Any]] = []
    for sp in trace.get("spans") or []:
        process_id = sp.get("processID")
        process = processes.get(process_id) if process_id else None
        service = process.get("serviceName") if process else ""
        # parent comes from references where refType=CHILD_OF
        parent = None
        for ref in sp.get("references", []) or []:
            if ref.get("refType") == "CHILD_OF":
                parent = ref.get("spanID")
                break
        tags = {t.get("key"): t.get("value") for t in sp.get("tags", []) or [] if t.get("key")}
        has_error = any(
            (t.get("key") == "error" and t.get("value")) for t in sp.get("tags", []) or []
        )
        out.append(
            {
                "span_id": sp.get("spanID") or "",
                "parent_span_id": parent,
                "name": sp.get("operationName", ""),
                "service": service or "",
                "start_ns": int(sp.get("startTime", 0)) * 1000,  # us → ns
                "duration_ms": int(sp.get("duration", 0)) / 1000.0,  # us → ms
                "status": "ERROR" if has_error else "OK",
                "tags": tags,
            }
        )
    return out


def _find_jaeger_root(spans: list[dict[str, Any]]) -> dict[str, Any] | None:
    for sp in spans:
        refs = sp.get("references") or []
        if not any(r.get("refType") == "CHILD_OF" for r in refs):
            return sp
    return spans[0] if spans else None


def _jaeger_span_has_error(sp: dict[str, Any]) -> bool:
    return any(
        t.get("key") == "error" and t.get("value")
        for t in sp.get("tags", []) or []
    )


# --------------------------------------------------------------------- handlers


async def run_get_trace(
    backend: TraceBackend | None,
    args: dict[str, Any],
    *,
    log_lookup=None,
) -> dict[str, Any]:
    """Fetch a trace, build the recursive tree, and best-effort attach logs.

    `log_lookup` is an optional callable: (trace_id) -> list[dict] returning
    audit rows that referenced the trace_id. If None, the `logs` field is null.
    """
    if backend is None:
        return errors.not_configured("trace", "TEMPO_URL or JAEGER_URL")
    from .schemas import GetTraceArgs

    try:
        a = GetTraceArgs.model_validate(args)
    except ValueError as e:
        return errors.invalid_arguments(str(e))

    try:
        spans = await backend.get_trace(a.trace_id)
    except httpx.HTTPStatusError as e:
        return errors.upstream_error(backend.backend_name, e.response.status_code, e.response.text)
    except (httpx.HTTPError, OSError) as e:
        return errors.backend_unreachable(backend.backend_name, e)

    if spans is None or not spans:
        return {
            "error": "trace_not_found",
            "trace_id": a.trace_id,
            "backend": backend.backend_name,
        }

    tree = build_span_tree(spans)
    logs: list[dict[str, Any]] | None = None
    if log_lookup is not None:
        try:
            logs = log_lookup(a.trace_id)
        except Exception:  # noqa: BLE001 — log lookup must never break trace fetch
            logs = None

    return {
        "trace_id": a.trace_id,
        "backend": backend.backend_name,
        "span_count": len(spans),
        "tree": tree,
        "logs": logs,
    }


async def run_find_traces(
    backend: TraceBackend | None,
    args: dict[str, Any],
    *,
    log_lookup=None,
) -> dict[str, Any]:
    if backend is None:
        return errors.not_configured("trace", "TEMPO_URL or JAEGER_URL")
    from .schemas import FindTracesArgs

    try:
        a = FindTracesArgs.model_validate(args)
        start = _parse_iso(a.start, "start")
        end = _parse_iso(a.end, "end")
    except ValueError as e:
        return errors.invalid_arguments(str(e))

    try:
        traces = await backend.find_traces(
            service=a.service,
            start=start,
            end=end,
            min_duration_ms=a.min_duration_ms,
            error_only=a.error_only,
            limit=a.limit,
        )
    except httpx.HTTPStatusError as e:
        return errors.upstream_error(backend.backend_name, e.response.status_code, e.response.text)
    except (httpx.HTTPError, OSError) as e:
        return errors.backend_unreachable(backend.backend_name, e)

    # Best-effort `has_logs` enrichment for the first 50 results (caps cost).
    if log_lookup is not None:
        for t in traces[:50]:
            tid = t.get("trace_id")
            try:
                t["has_logs"] = bool(log_lookup(tid)) if tid else False
            except Exception:  # noqa: BLE001
                t["has_logs"] = None

    return {
        "service": a.service,
        "window": {"start": a.start, "end": a.end},
        "count": len(traces),
        "traces": traces,
    }


async def run_get_slow_spans(
    backend: TraceBackend | None, args: dict[str, Any]
) -> dict[str, Any]:
    if backend is None:
        return errors.not_configured("trace", "TEMPO_URL or JAEGER_URL")
    from .schemas import GetSlowSpansArgs

    try:
        a = GetSlowSpansArgs.model_validate(args)
    except ValueError as e:
        return errors.invalid_arguments(str(e))

    try:
        spans = await backend.get_slow_spans(
            service=a.service, window=a.window, threshold_ms=a.threshold_ms
        )
    except httpx.HTTPStatusError as e:
        return errors.upstream_error(backend.backend_name, e.response.status_code, e.response.text)
    except (httpx.HTTPError, OSError) as e:
        return errors.backend_unreachable(backend.backend_name, e)

    return {
        "service": a.service,
        "window": a.window,
        "threshold_ms": a.threshold_ms,
        "spans": spans,
    }
