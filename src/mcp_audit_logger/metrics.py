"""Prometheus text-exposition rendering for the audit store.

We deliberately don't pull in `prometheus_client` — the audit store is the
source of truth, and re-counting in-process would either drift from the DB or
double-count after restarts. Instead we synthesize the exposition format
directly from `AuditStore.stats()` on each scrape.

Exposed series:

    mcp_audit_calls_total{tool, success}        counter
    mcp_audit_call_duration_seconds{tool, quantile}  summary (p50, p95)
    mcp_audit_call_duration_seconds_sum{tool}
    mcp_audit_call_duration_seconds_count{tool}
    mcp_audit_db_size_bytes                     gauge

The "counter" we emit is the all-time count from the DB. Prometheus expects
counters to be monotonically non-decreasing within a process; because the
underlying store is durable across restarts, this is fine. Operators running
`audit_purge` will see the counter drop — if that matters for their alerting,
they should set a `process_uptime_seconds`-aware recording rule rather than
treat this as a strict counter.
"""

from __future__ import annotations

from .storage import AuditStore

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape_label_value(s: str) -> str:
    # Prometheus exposition: backslash, double-quote, and newline must be escaped.
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(store: AuditStore) -> str:
    """Return a Prometheus text-exposition payload for `store`."""
    stats = store.stats()
    lines: list[str] = []

    lines.append("# HELP mcp_audit_calls_total Total tool calls audited, by tool and success.")
    lines.append("# TYPE mcp_audit_calls_total counter")
    for s in stats:
        tool = _escape_label_value(s["tool_name"])
        errors = int(s["error_count"])
        successes = int(s["call_count"]) - errors
        lines.append(f'mcp_audit_calls_total{{tool="{tool}",success="true"}} {successes}')
        lines.append(f'mcp_audit_calls_total{{tool="{tool}",success="false"}} {errors}')

    lines.append("# HELP mcp_audit_call_duration_seconds Per-tool call-latency summary.")
    lines.append("# TYPE mcp_audit_call_duration_seconds summary")
    for s in stats:
        tool = _escape_label_value(s["tool_name"])
        for q_label, q_key in (("0.5", "p50_duration_ms"), ("0.95", "p95_duration_ms")):
            v = s.get(q_key)
            if v is not None:
                lines.append(
                    f'mcp_audit_call_duration_seconds{{tool="{tool}",'
                    f'quantile="{q_label}"}} {v / 1000.0}'
                )
        avg_ms = float(s["avg_duration_ms"] or 0.0)
        count = int(s["call_count"])
        lines.append(
            f'mcp_audit_call_duration_seconds_sum{{tool="{tool}"}} '
            f"{avg_ms / 1000.0 * count}"
        )
        lines.append(f'mcp_audit_call_duration_seconds_count{{tool="{tool}"}} {count}')

    lines.append("# HELP mcp_audit_db_size_bytes Size of the audit SQLite file on disk.")
    lines.append("# TYPE mcp_audit_db_size_bytes gauge")
    lines.append(f"mcp_audit_db_size_bytes {store.db_size_bytes()}")

    return "\n".join(lines) + "\n"
