"""Prometheus HTTP API client and tool handlers.

Implements the three metrics tools:
    - obs_query_metric           — raw PromQL range query
    - obs_get_service_metrics    — derived RED metrics for a service
    - obs_list_instrumented_services — discovery via label values

The OTLP semantic-convention metric names are used by default; users can
override them via Config.metric_names (see config.py) — useful when the
upstream instrumentation reports metrics under different names.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from . import errors

# OpenTelemetry semantic-convention defaults. Override with Config.metric_names.
DEFAULT_METRIC_NAMES: dict[str, str] = {
    "request_duration": "http_server_request_duration_seconds",
    "request_count": "http_server_requests_total",
    "status_label": "http_response_status_code",
    "service_label": "service_name",
}


def resolve_metric_names(overrides: dict[str, str] | None) -> dict[str, str]:
    """Merge user overrides on top of the defaults."""
    out = dict(DEFAULT_METRIC_NAMES)
    if overrides:
        out.update({k: v for k, v in overrides.items() if v})
    return out


# --------------------------------------------------------------------- client


class PrometheusClient:
    """Thin async client around the Prometheus HTTP API.

    Uses an injected httpx.AsyncClient when provided (for tests with
    MockTransport); otherwise builds one per call. Connection lifecycle
    is short-lived: each method opens and closes a client unless
    `_client` was injected.
    """

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
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(base_url=self._url, timeout=self._timeout)

    async def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        client = self._make_client()
        owns = self._client is None
        try:
            url = path if self._client is None else f"{self._url}{path}"
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Prometheus returned {resp.status_code}: {resp.text[:200]}",
                    request=resp.request,
                    response=resp,
                )
            return resp.json()
        finally:
            if owns:
                await client.aclose()

    async def query_range(
        self, expr: str, start: datetime, end: datetime, step: str
    ) -> dict[str, Any]:
        params = {
            "query": expr,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step,
        }
        return await self._request("/api/v1/query_range", params)

    async def query_instant(
        self, expr: str, *, time: datetime | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"query": expr}
        if time is not None:
            params["time"] = time.timestamp()
        return await self._request("/api/v1/query", params)

    async def label_values(self, label: str, match_metric: str) -> list[str]:
        data = await self._request(
            f"/api/v1/label/{label}/values", {"match[]": match_metric}
        )
        result = data.get("data") or []
        return [str(v) for v in result if v]


# --------------------------------------------------------------------- helpers


def _parse_iso(s: str, field: str) -> datetime:
    try:
        # fromisoformat handles "2024-01-01T00:00:00+00:00" and "2024-01-01T00:00:00Z"
        # in 3.11+; in 3.10 we strip the trailing Z manually.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"Invalid ISO8601 in '{field}': {s!r} ({e})") from e


def _service_selector(service_label: str, service: str) -> str:
    """Build a Prometheus label matcher: service_label="service" """
    # Single quotes inside service must not break out; service_label is fixed.
    safe = service.replace('"', '\\"')
    return f'{service_label}="{safe}"'


# --------------------------------------------------------------------- tool handlers


async def run_query_metric(client: PrometheusClient | None, args: dict[str, Any]) -> dict[str, Any]:
    if client is None:
        return errors.not_configured("prometheus", "PROMETHEUS_URL")
    from .schemas import QueryMetricArgs

    try:
        a = QueryMetricArgs.model_validate(args)
        start = _parse_iso(a.start, "start")
        end = _parse_iso(a.end, "end")
    except ValueError as e:
        return errors.invalid_arguments(str(e))

    try:
        data = await client.query_range(a.expr, start, end, a.step)
    except httpx.HTTPStatusError as e:
        return errors.upstream_error("prometheus", e.response.status_code, e.response.text)
    except (httpx.HTTPError, OSError) as e:
        return errors.backend_unreachable("prometheus", e)
    return {
        "expr": a.expr,
        "range": {"start": a.start, "end": a.end, "step": a.step},
        "data": data,
    }


async def run_get_service_metrics(
    client: PrometheusClient | None,
    args: dict[str, Any],
    metric_names: dict[str, str],
) -> dict[str, Any]:
    if client is None:
        return errors.not_configured("prometheus", "PROMETHEUS_URL")
    from .schemas import GetServiceMetricsArgs

    try:
        a = GetServiceMetricsArgs.model_validate(args)
    except ValueError as e:
        return errors.invalid_arguments(str(e))

    names = resolve_metric_names(metric_names)
    sel = _service_selector(names["service_label"], a.service)
    win = a.window
    bucket_metric = f'{names["request_duration"]}_bucket'
    count_metric = names["request_count"]
    status_label = names["status_label"]

    # Construct PromQL fragments.
    request_rate_q = f'sum(rate({count_metric}{{{sel}}}[{win}]))'
    error_rate_q = (
        f'sum(rate({count_metric}{{{sel}, {status_label}=~"5.."}}[{win}])) '
        f'/ clamp_min(sum(rate({count_metric}{{{sel}}}[{win}])), 1)'
    )
    p50_q = (
        f'histogram_quantile(0.5, sum(rate({bucket_metric}{{{sel}}}[{win}])) by (le)) * 1000'
    )
    p95_q = (
        f'histogram_quantile(0.95, sum(rate({bucket_metric}{{{sel}}}[{win}])) by (le)) * 1000'
    )
    p99_q = (
        f'histogram_quantile(0.99, sum(rate({bucket_metric}{{{sel}}}[{win}])) by (le)) * 1000'
    )

    async def _scalar(q: str) -> float | None:
        try:
            data = await client.query_instant(q)
        except httpx.HTTPError:
            return None
        return _extract_scalar(data)

    try:
        request_rate = await _scalar(request_rate_q)
        error_rate = await _scalar(error_rate_q)
        p50 = await _scalar(p50_q)
        p95 = await _scalar(p95_q)
        p99 = await _scalar(p99_q)
    except (httpx.HTTPError, OSError) as e:
        return errors.backend_unreachable("prometheus", e)

    return {
        "service": a.service,
        "window": a.window,
        "request_rate_rps": request_rate,
        "error_rate": error_rate,
        "latency_ms": {"p50": p50, "p95": p95, "p99": p99},
        "metric_names": names,
        "queries": {
            "request_rate": request_rate_q,
            "error_rate": error_rate_q,
            "latency_p99": p99_q,
        },
    }


async def run_list_instrumented_services(
    client: PrometheusClient | None,
    args: dict[str, Any],
    metric_names: dict[str, str],
) -> dict[str, Any]:
    if client is None:
        return errors.not_configured("prometheus", "PROMETHEUS_URL")
    from .schemas import ListInstrumentedServicesArgs

    try:
        a = ListInstrumentedServicesArgs.model_validate(args)
    except ValueError as e:
        return errors.invalid_arguments(str(e))

    names = resolve_metric_names(metric_names)
    base = a.base_metric or names["request_count"]
    label = names["service_label"]
    try:
        services = await client.label_values(label, base)
    except (httpx.HTTPError, OSError) as e:
        return errors.backend_unreachable("prometheus", e)
    return {
        "services": sorted(services),
        "base_metric": base,
        "label": label,
    }


def _extract_scalar(prom_response: dict[str, Any]) -> float | None:
    """Pull a single numeric value out of a Prometheus instant query response.

    Format: {"data": {"resultType": "vector"|"scalar",
                       "result": [{"metric": {...}, "value": [ts, "0.05"]}, ...]}}
    Returns None if the result is empty, missing, or not parseable as a float.
    """
    data = prom_response.get("data") or {}
    result = data.get("result")
    if data.get("resultType") == "scalar" and isinstance(result, list) and len(result) == 2:
        try:
            return float(result[1])
        except (TypeError, ValueError):
            return None
    if not isinstance(result, list) or not result:
        return None
    first = result[0]
    value = first.get("value") if isinstance(first, dict) else None
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        v = float(value[1])
    except (TypeError, ValueError):
        return None
    # NaN sneaks through histogram_quantile when no buckets observed.
    if v != v:  # NaN check
        return None
    return v
