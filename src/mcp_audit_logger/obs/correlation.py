"""obs_investigate — composite incident-triage tool.

Fans out 4 calls in parallel via anyio.create_task_group:
    1. Metrics deltas (current window vs prior equivalent window)
    2. Anomalous traces (errored + slowest)
    3. Log sample from existing AuditStore
    4. Active and recently-resolved alerts

Each step is wrapped in try/except so a single backend failure leaves the
others' results intact. Failures are appended to `errors[]` so the caller
can see exactly what's missing. Total wall-clock latency = max of the four
backend calls, not the sum.

The `summary` field is generated deterministically from the data using
heuristics — no LLM, no randomness, fully reproducible.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import anyio
import httpx

from . import errors
from .alerts import AlertManagerClient
from .prometheus import PrometheusClient, _service_selector, resolve_metric_names
from .traces import TraceBackend

# --------------------------------------------------------------------- main


async def investigate(
    *,
    service: str,
    start_iso: str,
    end_iso: str,
    prom: PrometheusClient | None,
    trace: TraceBackend | None,
    alerts: AlertManagerClient | None,
    store: Any | None,
    metric_names: dict[str, str],
) -> dict[str, Any]:
    try:
        start_dt = _parse_iso(start_iso)
        end_dt = _parse_iso(end_iso)
    except ValueError as e:
        return errors.invalid_arguments(str(e))

    if end_dt <= start_dt:
        return errors.invalid_arguments("`end` must be after `start`.")

    window_secs = (end_dt - start_dt).total_seconds()
    prior_start = start_dt - timedelta(seconds=window_secs)
    prior_end = start_dt

    out: dict[str, Any] = {
        "service": service,
        "window": {"start": start_iso, "end": end_iso},
        "prior_window": {
            "start": prior_start.isoformat(),
            "end": prior_end.isoformat(),
        },
        "metrics_delta": None,
        "anomalous_traces": None,
        "log_sample": None,
        "alerts": None,
        "summary": "",
        "errors": [],
    }

    names = resolve_metric_names(metric_names)

    async def _step_metrics() -> None:
        if prom is None:
            out["errors"].append(
                {"step": "metrics", "backend": "prometheus", "message": "not configured"}
            )
            return
        try:
            out["metrics_delta"] = await _compute_metrics_delta(
                prom, service, names, start_dt, end_dt, prior_start, prior_end, window_secs
            )
        except (httpx.HTTPError, OSError) as e:
            out["errors"].append(
                {"step": "metrics", "backend": "prometheus", "message": f"{type(e).__name__}: {e}"}
            )

    async def _step_traces() -> None:
        if trace is None:
            out["errors"].append(
                {"step": "traces", "backend": "trace", "message": "not configured"}
            )
            return
        try:
            errored = await trace.find_traces(
                service=service,
                start=start_dt,
                end=end_dt,
                min_duration_ms=None,
                error_only=True,
                limit=20,
            )
            slow = await trace.find_traces(
                service=service,
                start=start_dt,
                end=end_dt,
                min_duration_ms=None,
                error_only=False,
                limit=50,
            )
            slow_sorted = sorted(slow, key=lambda t: t.get("duration_ms", 0), reverse=True)
            out["anomalous_traces"] = {
                "errored": errored,
                "slowest": slow_sorted[:3],
            }
        except (httpx.HTTPError, OSError) as e:
            out["errors"].append(
                {
                    "step": "traces",
                    "backend": getattr(trace, "backend_name", "trace"),
                    "message": f"{type(e).__name__}: {e}",
                }
            )

    async def _step_logs() -> None:
        if store is None:
            out["log_sample"] = []
            return
        try:
            # AuditStore is sync SQLite; safe to call directly. In_range expects
            # epoch-second floats matching the existing audit schema.
            rows = store.in_range(
                start_ts=start_dt.timestamp(),
                end_ts=end_dt.timestamp(),
                limit=100,
            )
            # Filter best-effort: include rows whose tool_name OR arguments
            # mention the service. Audit records aren't service-tagged, so this
            # is permissive — keep both error and warning-shaped rows.
            keyword = service.lower()
            picked = []
            for r in rows:
                hay = f"{r.get('tool_name','')} {r.get('arguments','')}".lower()
                if keyword in hay or not r.get("success", True):
                    picked.append(r)
                if len(picked) >= 20:
                    break
            out["log_sample"] = picked
        except Exception as e:  # noqa: BLE001 — store is local; surface the error
            out["errors"].append(
                {"step": "logs", "backend": "audit_store", "message": f"{type(e).__name__}: {e}"}
            )

    async def _step_alerts() -> None:
        if alerts is None:
            out["errors"].append(
                {"step": "alerts", "backend": "alertmanager", "message": "not configured"}
            )
            return
        try:
            active = await alerts.active_alerts(service)
            since = end_dt - timedelta(hours=1)
            recent = await alerts.recently_resolved(service, since=since, until=end_dt)
            out["alerts"] = {"active": active, "recently_resolved": recent}
        except (httpx.HTTPError, OSError) as e:
            out["errors"].append(
                {
                    "step": "alerts",
                    "backend": "alertmanager",
                    "message": f"{type(e).__name__}: {e}",
                }
            )

    async with anyio.create_task_group() as tg:
        tg.start_soon(_step_metrics)
        tg.start_soon(_step_traces)
        tg.start_soon(_step_logs)
        tg.start_soon(_step_alerts)

    out["summary"] = build_summary(out, service)
    return out


# --------------------------------------------------------------------- helpers


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _compute_metrics_delta(
    prom: PrometheusClient,
    service: str,
    names: dict[str, str],
    start: datetime,
    end: datetime,
    prior_start: datetime,
    prior_end: datetime,
    window_secs: float,
) -> dict[str, Any]:
    """Run instant queries at end-of-window for current and prior periods."""
    sel = _service_selector(names["service_label"], service)
    win = _seconds_to_prom_duration(int(window_secs))
    bucket_metric = f'{names["request_duration"]}_bucket'
    count_metric = names["request_count"]
    status_label = names["status_label"]

    request_rate_q = f'sum(rate({count_metric}{{{sel}}}[{win}]))'
    error_rate_q = (
        f'sum(rate({count_metric}{{{sel}, {status_label}=~"5.."}}[{win}])) '
        f'/ clamp_min(sum(rate({count_metric}{{{sel}}}[{win}])), 1)'
    )
    p99_q = (
        f'histogram_quantile(0.99, sum(rate({bucket_metric}{{{sel}}}[{win}])) by (le)) * 1000'
    )

    async def _scalar(q: str, at: datetime) -> float | None:
        try:
            data = await prom.query_instant(q, time=at)
        except httpx.HTTPError:
            return None
        from .prometheus import _extract_scalar

        return _extract_scalar(data)

    cur_rr, cur_er, cur_p99 = (
        await _scalar(request_rate_q, end),
        await _scalar(error_rate_q, end),
        await _scalar(p99_q, end),
    )
    prior_rr, prior_er, prior_p99 = (
        await _scalar(request_rate_q, prior_end),
        await _scalar(error_rate_q, prior_end),
        await _scalar(p99_q, prior_end),
    )

    return {
        "request_rate_rps": _delta(cur_rr, prior_rr),
        "error_rate": _delta(cur_er, prior_er),
        "latency_p99_ms": _delta(cur_p99, prior_p99),
        "queries": {
            "request_rate": request_rate_q,
            "error_rate": error_rate_q,
            "latency_p99": p99_q,
        },
    }


def _delta(current: float | None, prior: float | None) -> dict[str, Any]:
    """Build a {current, prior, delta_pct} dict; tolerant of Nones and zeros."""
    out: dict[str, Any] = {"current": current, "prior": prior, "delta_pct": None}
    if current is None or prior is None:
        return out
    if prior == 0:
        out["delta_pct"] = float("inf") if current > 0 else 0.0
        return out
    out["delta_pct"] = round((current - prior) / prior * 100.0, 2)
    return out


def _seconds_to_prom_duration(secs: int) -> str:
    if secs >= 3600 and secs % 3600 == 0:
        return f"{secs // 3600}h"
    if secs >= 60 and secs % 60 == 0:
        return f"{secs // 60}m"
    return f"{max(1, secs)}s"


# --------------------------------------------------------------------- summary


def build_summary(out: dict[str, Any], service: str) -> str:
    """Generate a 1-3 sentence English summary deterministically from `out`.

    Heuristics, in order of priority:
      1. Error-rate spike (>100% delta) is the headline.
      2. If errored traces share a dominant operation (>50%), call it out.
      3. Latency-p99 spike if no error spike present.
      4. Log keywords (timeout, refused, 503) escalate to "Logs indicate ...".
      5. Active alerts get a sentence.
      6. If everything looks normal, say so.
    """
    parts: list[str] = []
    md = out.get("metrics_delta") or {}
    er = (md.get("error_rate") or {}) if isinstance(md, dict) else {}
    p99 = (md.get("latency_p99_ms") or {}) if isinstance(md, dict) else {}

    er_delta = er.get("delta_pct")
    if isinstance(er_delta, (int, float)) and er_delta == float("inf"):
        parts.append(f"Error rate appeared (prior was 0) at {er.get('current'):.1%}.")
    elif isinstance(er_delta, (int, float)) and er_delta > 100:
        mult = (er_delta / 100) + 1
        parts.append(f"Error rate spiked {mult:.1f}x vs prior window.")

    # Dominant errored operation
    at = out.get("anomalous_traces") or {}
    errored = at.get("errored") or []
    if errored:
        names_count: dict[str, int] = {}
        for t in errored:
            names_count[t.get("root_name", "")] = names_count.get(t.get("root_name", ""), 0) + 1
        if names_count:
            top_name, top_count = max(names_count.items(), key=lambda kv: kv[1])
            ratio = top_count / len(errored)
            if ratio > 0.5 and top_name:
                parts.append(
                    f"{int(ratio * 100)}% of errored traces share operation: {top_name}."
                )

    # Latency-only signal
    if not parts and isinstance(p99.get("delta_pct"), (int, float)) and p99["delta_pct"] > 50:
        parts.append(f"Latency p99 spiked {p99['delta_pct']:.0f}% vs prior window.")

    # Log keywords
    logs = out.get("log_sample") or []
    keywords = ("timeout", "connection refused", "unavailable", "503", "502", "504")
    log_hits: dict[str, int] = {}
    for r in logs:
        text = (str(r.get("error", "")) + " " + str(r.get("response", ""))).lower()
        for kw in keywords:
            if kw in text:
                log_hits[kw] = log_hits.get(kw, 0) + 1
                break
    if log_hits:
        top_kw = max(log_hits.items(), key=lambda kv: kv[1])[0]
        total = sum(log_hits.values())
        parts.append(f'Log lines indicate "{top_kw}" appearing in {total} record(s).')

    # Alerts
    alerts = out.get("alerts") or {}
    active = alerts.get("active") if isinstance(alerts, dict) else None
    if active:
        names = [a.get("labels", {}).get("alertname", "?") for a in active[:3]]
        parts.append(f"Active alerts: {', '.join(names)}.")

    if not parts:
        backends_failed = {e.get("step") for e in out.get("errors", [])}
        if backends_failed:
            failed_str = ", ".join(sorted(b for b in backends_failed if b))
            parts.append(
                f"Partial data only ({failed_str} unavailable); "
                f"no anomalies detected for {service} in window."
            )
        else:
            parts.append(f"No anomalies detected for {service} in window.")

    return " ".join(parts)
