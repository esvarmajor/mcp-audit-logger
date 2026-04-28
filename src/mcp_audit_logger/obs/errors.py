"""Structured error builders for observability tools.

Tools never throw across the MCP boundary — they return a JSON dict with a
top-level `error` key. The dispatch layer in `tools.run_obs_tool` detects
this shape and sets `isError=True` on the resulting CallToolResult.
"""

from __future__ import annotations

from typing import Any


def not_configured(backend: str, env_var: str) -> dict[str, Any]:
    return {
        "error": "not_configured",
        "backend": backend,
        "message": f"Set {env_var} to enable {backend} queries",
    }


def backend_unreachable(backend: str, exc: Exception) -> dict[str, Any]:
    return {
        "error": "backend_unreachable",
        "backend": backend,
        "message": f"{type(exc).__name__}: {exc}",
    }


def upstream_error(backend: str, status: int, body: str) -> dict[str, Any]:
    return {
        "error": "upstream_error",
        "backend": backend,
        "status": status,
        "message": body[:500],
    }


def invalid_arguments(message: str) -> dict[str, Any]:
    return {"error": "invalid_arguments", "message": message}


def is_error(payload: Any) -> bool:
    """True if `payload` is a dict with a top-level `error` key (our envelope)."""
    return isinstance(payload, dict) and "error" in payload
