"""The audit-logger MCP server.

This module builds the low-level `mcp.server.lowlevel.Server` instance that our
Streamable HTTP transport wraps. It does two jobs:

    1. Transparently proxies `tools/list` and `tools/call` to a downstream MCP
       server, persisting every call to the AuditStore.
    2. Exposes its own `audit_*` query tools so an agent can introspect the log.

The audit tools are namespaced with the `audit_` prefix so they can never
collide with downstream tool names. If a downstream happens to expose a tool
also named `audit_*`, the audit tool wins — callers can still reach the
downstream one via whatever name it uses (our bookkeeping only kicks in for
exact matches in the prefix set).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from pydantic import BaseModel, Field

from .proxy import DownstreamProxy
from .storage import AuditStore

log = logging.getLogger(__name__)


# --------------------------------------------------------------- audit tool schemas


class _RecentArgs(BaseModel):
    limit: int = Field(default=50, ge=1, le=1000, description="Max rows to return.")


class _ByToolArgs(BaseModel):
    tool_name: str = Field(description="The tool name to filter by.")
    limit: int = Field(default=50, ge=1, le=1000)


class _FailedArgs(BaseModel):
    limit: int = Field(default=50, ge=1, le=1000)


AUDIT_TOOLS: list[types.Tool] = [
    types.Tool(
        name="audit_get_recent_calls",
        description=(
            "Return the most recent N tool calls recorded by the audit logger "
            "(newest first). Includes arguments, response, duration, and success state."
        ),
        inputSchema=_RecentArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_get_calls_by_tool",
        description="Return the most recent N calls matching the given tool name.",
        inputSchema=_ByToolArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_get_failed_calls",
        description="Return the most recent N failed tool calls (success = false).",
        inputSchema=_FailedArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_get_call_stats",
        description=(
            "Return per-tool aggregate stats: call count, avg/min/max duration (ms), "
            "error count, and error rate. Ordered by call count descending."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
]
AUDIT_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in AUDIT_TOOLS)


# ---------------------------------------------------------------------- builder


def build_server(
    *,
    proxy: DownstreamProxy | None,
    store: AuditStore,
    server_name: str = "mcp-audit-logger",
) -> Server:
    """Construct the low-level `Server` with handlers wired in."""

    srv: Server = Server(server_name)

    @srv.list_tools()
    async def _list_tools() -> list[types.Tool]:
        tools: list[types.Tool] = list(AUDIT_TOOLS)
        if proxy is not None:
            try:
                downstream = await proxy.list_tools()
                tools.extend(downstream)
            except Exception:
                log.exception("proxy.list_tools_failed")
        return tools

    @srv.call_tool()
    async def _call_tool(
        name: str,
        arguments: dict[str, Any] | None,
    ) -> list[types.ContentBlock]:
        if name in AUDIT_TOOL_NAMES:
            return _run_audit_tool(store, name, arguments or {})
        if proxy is None:
            raise ValueError(
                f"Unknown tool: {name!r}. No downstream server is configured, so only "
                f"audit_* tools are available."
            )
        return await _proxied_call(proxy, store, name, arguments)

    return srv


# ----------------------------------------------------------- audit tool execution


def _run_audit_tool(
    store: AuditStore, name: str, arguments: dict[str, Any]
) -> list[types.ContentBlock]:
    if name == "audit_get_recent_calls":
        args = _RecentArgs.model_validate(arguments)
        out: Any = store.recent(limit=args.limit)
    elif name == "audit_get_calls_by_tool":
        args2 = _ByToolArgs.model_validate(arguments)
        out = store.by_tool(tool_name=args2.tool_name, limit=args2.limit)
    elif name == "audit_get_failed_calls":
        args3 = _FailedArgs.model_validate(arguments)
        out = store.failed(limit=args3.limit)
    elif name == "audit_get_call_stats":
        out = store.stats()
    else:  # pragma: no cover — guarded by AUDIT_TOOL_NAMES membership
        raise ValueError(f"Unknown audit tool: {name}")

    return [types.TextContent(type="text", text=json.dumps(out, indent=2, default=str))]


# --------------------------------------------------------------- proxied calls


async def _proxied_call(
    proxy: DownstreamProxy,
    store: AuditStore,
    name: str,
    arguments: dict[str, Any] | None,
) -> list[types.ContentBlock]:
    """Forward a tool call to the downstream server and record the audit row.

    The record is always written — successful calls, downstream-reported
    errors (`isError=True`), and exceptions we catch here all end up in the log.
    """
    t0 = time.time()
    success = False
    error: str | None = None
    response_payload: Any = None
    try:
        result = await proxy.call_tool(name, arguments)
        response_payload = (
            result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        )
        success = not getattr(result, "isError", False)
        if not success:
            error = _extract_error_text(result)
        return list(getattr(result, "content", []) or [])
    except Exception as exc:
        success = False
        error = f"{type(exc).__name__}: {exc}"
        response_payload = {"exception": error}
        raise
    finally:
        t1 = time.time()
        try:
            store.log_call(
                ts_start=t0,
                ts_end=t1,
                tool_name=name,
                arguments=arguments,
                response=response_payload,
                success=success,
                error=error,
                downstream_target=proxy.target_label,
            )
        except Exception:
            log.exception("audit.log_call_failed", extra={"tool_name": name})
        log.info(
            "tool.proxied",
            extra={
                "tool_name": name,
                "duration_ms": (t1 - t0) * 1000.0,
                "success": success,
                "downstream": proxy.target_label,
            },
        )


def _extract_error_text(result: Any) -> str | None:
    """Best-effort: pull the visible text out of an errored CallToolResult."""
    content = getattr(result, "content", None)
    if not content:
        return None
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts) if parts else None
