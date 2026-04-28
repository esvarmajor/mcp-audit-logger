"""Observability tools: Prometheus metrics, distributed traces, alerts, correlation.

Exposes a parallel namespace (`obs_*` tools) to the existing `audit_*` tools.
All clients are instantiated from Config in `__main__._run` and threaded into
`build_server`. If a backend client is None, the corresponding tool returns
a structured `not_configured` error rather than raising.
"""

from .alerts import AlertManagerClient
from .prometheus import PrometheusClient
from .tools import OBS_TOOL_NAMES, OBS_TOOLS, run_obs_tool
from .traces import JaegerClient, TempoClient, TraceBackend, build_span_tree

__all__ = [
    "OBS_TOOLS",
    "OBS_TOOL_NAMES",
    "run_obs_tool",
    "PrometheusClient",
    "TempoClient",
    "JaegerClient",
    "TraceBackend",
    "AlertManagerClient",
    "build_span_tree",
]
