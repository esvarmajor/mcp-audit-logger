"""Pydantic input schemas for the obs_* tools.

Mirror the convention used by the existing audit_* tools (server.py:36-75):
BaseModel + Field(description=...), `inputSchema=Model.model_json_schema()`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class QueryMetricArgs(BaseModel):
    expr: str = Field(description="PromQL expression to evaluate over the range.")
    start: str = Field(description="ISO8601 timestamp for range start (UTC if no offset).")
    end: str = Field(description="ISO8601 timestamp for range end (UTC if no offset).")
    step: str = Field(
        default="30s",
        description='Resolution step (Prometheus duration syntax, e.g. "30s", "1m").',
    )


class GetServiceMetricsArgs(BaseModel):
    service: str = Field(description="Service name to query metrics for.")
    window: str = Field(
        default="5m",
        description='Lookback window (Prometheus duration syntax, e.g. "5m", "1h").',
    )


class ListInstrumentedServicesArgs(BaseModel):
    base_metric: str | None = Field(
        default=None,
        description=(
            "Metric to introspect for service label values. "
            "Defaults to the configured request-count metric."
        ),
    )


class GetTraceArgs(BaseModel):
    trace_id: str = Field(description="The trace ID to fetch (hex-encoded).")


class FindTracesArgs(BaseModel):
    service: str = Field(description="Service name to filter traces by.")
    start: str = Field(description="ISO8601 timestamp for window start.")
    end: str = Field(description="ISO8601 timestamp for window end.")
    min_duration_ms: int | None = Field(
        default=None, description="Optional: only return traces with duration >= this many ms."
    )
    error_only: bool = Field(
        default=False,
        description="If true, return only traces that contain at least one error span.",
    )
    limit: int = Field(default=20, ge=1, le=200, description="Max traces to return.")


class GetSlowSpansArgs(BaseModel):
    service: str = Field(description="Service name to query.")
    window: str = Field(
        default="5m", description='Lookback window relative to now (e.g. "5m", "1h").'
    )
    threshold_ms: int = Field(
        ge=1, description="Only include spans whose duration exceeds this threshold (ms)."
    )


class InvestigateArgs(BaseModel):
    service: str = Field(description="Service to investigate.")
    start: str = Field(description="ISO8601 timestamp for incident window start.")
    end: str = Field(description="ISO8601 timestamp for incident window end.")
