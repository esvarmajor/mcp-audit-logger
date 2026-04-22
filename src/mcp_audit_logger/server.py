"""The audit-logger MCP server.

Builds the low-level `mcp.server.lowlevel.Server` instance that the Streamable
HTTP transport wraps. Handler registration uses the SDK's constructor-kwarg API
(`on_list_tools=`, `on_call_tool=`) introduced in MCP SDK ≥ 1.6.

The server does two jobs:

    1. Proxies `tools/list` and `tools/call` to a downstream MCP server,
       persisting every call to the AuditStore.
    2. Exposes `audit_*` query tools so an agent can introspect call history.

The `audit_` namespace ensures these tools can never clash with downstream names.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from pydantic import BaseModel, Field, ValidationError

from .proxy import DownstreamProxy
from .storage import AuditStore

log = logging.getLogger(__name__)


# --------------------------------------------------------------- argument schemas


class _RecentArgs(BaseModel):
    limit: int = Field(default=50, ge=1, le=1000)


class _ByToolArgs(BaseModel):
    tool_name: str
    limit: int = Field(default=50, ge=1, le=1000)


class _FailedArgs(BaseModel):
    limit: int = Field(default=50, ge=1, le=1000)


class _RangeArgs(BaseModel):
    start_ts: float = Field(description="Unix timestamp (seconds) range start (inclusive).")
    end_ts: float = Field(description="Unix timestamp (seconds) range end (inclusive).")
    limit: int = Field(default=200, ge=1, le=5000)


class _SearchArgsModel(BaseModel):
    pattern: str = Field(
        description="SQL LIKE pattern searched inside the serialised arguments JSON. "
        'Use % as wildcard — e.g. "%example.com%" or "%some_key%".'
    )
    limit: int = Field(default=50, ge=1, le=1000)


class _ExportArgs(BaseModel):
    limit: int = Field(default=500, ge=1, le=10000)
    since_id: int = Field(default=0, ge=0, description="Only export rows with id > this value.")


class _PurgeArgs(BaseModel):
    before_ts: float = Field(
        description="Delete all rows whose ts_start is strictly before this Unix timestamp."
    )
    dry_run: bool = Field(
        default=True,
        description="When True (default), report how many rows WOULD be deleted without deleting.",
    )


# --------------------------------------------------------------- tool definitions

AUDIT_TOOLS: list[types.Tool] = [
    types.Tool(
        name="audit_get_recent_calls",
        description=(
            "Return the most recent N tool calls (newest first). "
            "Includes arguments, response, duration, and success/failure."
        ),
        inputSchema=_RecentArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_get_calls_by_tool",
        description="Return the most recent N calls filtered to a specific tool name.",
        inputSchema=_ByToolArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_get_failed_calls",
        description="Return the most recent N failed tool calls (success = false only).",
        inputSchema=_FailedArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_get_call_stats",
        description=(
            "Per-tool aggregate stats: call count, avg/min/max duration (ms), "
            "error count, and error rate. Ordered by call count descending."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    types.Tool(
        name="audit_get_calls_in_range",
        description="Return calls whose start timestamp falls within [start_ts, end_ts].",
        inputSchema=_RangeArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_search_arguments",
        description=(
            "Full-text LIKE search over the serialised argument JSON of every call. "
            'Use SQL wildcard syntax: e.g. pattern="%example.com%".'
        ),
        inputSchema=_SearchArgsModel.model_json_schema(),
    ),
    types.Tool(
        name="audit_export_jsonl",
        description=(
            "Export up to `limit` audit rows as JSONL text (one JSON object per line), "
            "optionally filtered to rows with id > since_id. "
            "Useful for piping into analysis tools."
        ),
        inputSchema=_ExportArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_purge",
        description=(
            "Delete audit rows older than `before_ts`. "
            "Set dry_run=false to actually delete; defaults to dry_run=true (safe preview). "
            "Returns the count of affected rows."
        ),
        inputSchema=_PurgeArgs.model_json_schema(),
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
    """Return a configured low-level Server instance.

    Handlers are registered via constructor kwargs (the only registration
    mechanism available in SDK ≥ 1.6 for the low-level Server class).
    """

    async def _on_list_tools(
        ctx: Any,
        params: Any,
    ) -> types.ListToolsResult:
        tools: list[types.Tool] = list(AUDIT_TOOLS)
        if proxy is not None:
            try:
                downstream = await proxy.list_tools()
                tools.extend(downstream)
            except Exception:
                log.exception("proxy.list_tools_failed")
        return types.ListToolsResult(tools=tools)

    async def _on_call_tool(
        ctx: Any,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        name = params.name
        arguments = dict(params.arguments) if params.arguments else {}

        if name in AUDIT_TOOL_NAMES:
            try:
                content = _run_audit_tool(store, name, arguments)
                return types.CallToolResult(content=content)
            except (ValidationError, ValueError) as exc:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=f"Invalid arguments: {exc}")],
                    isError=True,
                )

        if proxy is None:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=(
                            f"Unknown tool {name!r}. No downstream server is configured — "
                            "only audit_* tools are available."
                        ),
                    )
                ],
                isError=True,
            )

        return await _proxied_call(proxy, store, name, arguments or None)

    return Server(
        server_name,
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )


# ----------------------------------------------------------- audit tool dispatch


def _run_audit_tool(
    store: AuditStore, name: str, arguments: dict[str, Any]
) -> list[types.TextContent]:
    if name == "audit_get_recent_calls":
        a = _RecentArgs.model_validate(arguments)
        out: Any = store.recent(limit=a.limit)
    elif name == "audit_get_calls_by_tool":
        a2 = _ByToolArgs.model_validate(arguments)
        out = store.by_tool(tool_name=a2.tool_name, limit=a2.limit)
    elif name == "audit_get_failed_calls":
        a3 = _FailedArgs.model_validate(arguments)
        out = store.failed(limit=a3.limit)
    elif name == "audit_get_call_stats":
        out = store.stats()
    elif name == "audit_get_calls_in_range":
        a4 = _RangeArgs.model_validate(arguments)
        out = store.in_range(start_ts=a4.start_ts, end_ts=a4.end_ts, limit=a4.limit)
    elif name == "audit_search_arguments":
        a5 = _SearchArgsModel.model_validate(arguments)
        out = store.search_arguments(pattern=a5.pattern, limit=a5.limit)
    elif name == "audit_export_jsonl":
        a6 = _ExportArgs.model_validate(arguments)
        rows = store.export(limit=a6.limit, since_id=a6.since_id)
        lines = "\n".join(json.dumps(r, default=str) for r in rows)
        return [types.TextContent(type="text", text=lines or "(no records)")]
    elif name == "audit_purge":
        a7 = _PurgeArgs.model_validate(arguments)
        count = store.purge(before_ts=a7.before_ts, dry_run=a7.dry_run)
        verb = "would delete" if a7.dry_run else "deleted"
        return [
            types.TextContent(type="text", text=json.dumps({"rows_affected": count, "verb": verb}))
        ]
    else:  # pragma: no cover
        raise ValueError(f"Unknown audit tool: {name}")

    return [types.TextContent(type="text", text=json.dumps(out, indent=2, default=str))]


# --------------------------------------------------------------- proxied calls


async def _proxied_call(
    proxy: DownstreamProxy,
    store: AuditStore,
    name: str,
    arguments: dict[str, Any] | None,
) -> types.CallToolResult:
    """Forward a call to the downstream server and persist an audit row.

    Always writes a row — successful calls, downstream-signalled errors
    (`isError=True`), and network/exception failures all end up logged.
    """
    t0 = time.time()
    success = False
    error: str | None = None
    response_payload: Any = None
    is_error = False

    try:
        result = await proxy.call_tool(name, arguments)
        response_payload = (
            result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        )
        is_error = bool(getattr(result, "isError", False))
        success = not is_error
        if is_error:
            error = _extract_error_text(result)
        return types.CallToolResult(
            content=list(getattr(result, "content", []) or []),
            isError=is_error or None,
        )
    except Exception as exc:
        success = False
        is_error = True
        error = f"{type(exc).__name__}: {exc}"
        response_payload = {"exception": error}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Downstream error: {error}")],
            isError=True,
        )
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
                "tool": name,
                "duration_ms": round((t1 - t0) * 1000, 2),
                "success": success,
                "downstream": proxy.target_label,
            },
        )


def _extract_error_text(result: Any) -> str | None:
    content = getattr(result, "content", None)
    if not content:
        return None
    parts = [block.text for block in content if hasattr(block, "text") and block.text]
    return "\n".join(parts) if parts else None
