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

from .context import client_info_var
from .proxy import DownstreamProxy
from .storage import AuditStore

log = logging.getLogger(__name__)


# --------------------------------------------------------------- argument schemas


class _RecentArgs(BaseModel):
    limit: int = Field(default=50, ge=1, le=1000)


class _GetByIdArgs(BaseModel):
    call_id: int = Field(ge=1, description="The primary-key id of the audit row to fetch.")


class _ByToolArgs(BaseModel):
    tool_name: str
    limit: int = Field(default=50, ge=1, le=1000)


class _ByClientArgs(BaseModel):
    client_pattern: str = Field(
        description="SQL LIKE pattern matched against client_info — e.g. %10.0.0.5%."
    )
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
    tool_name: str | None = Field(
        default=None,
        description="Optional exact-match filter on tool_name. Narrow large search results.",
    )
    include_response: bool = Field(
        default=False,
        description="If true, also match the pattern against the response payload column.",
    )
    limit: int = Field(default=50, ge=1, le=1000)


class _ExportArgs(BaseModel):
    limit: int = Field(default=500, ge=1, le=10000)
    since_id: int = Field(default=0, ge=0, description="Only export rows with id > this value.")


class _SlowestArgs(BaseModel):
    limit: int = Field(default=20, ge=1, le=1000)


class _CountArgs(BaseModel):
    tool_name: str | None = Field(default=None, description="Optional exact-match filter.")
    success: bool | None = Field(
        default=None,
        description="Optional outcome filter — true=successes only, false=failures only.",
    )
    since_ts: float | None = Field(
        default=None,
        description="Optional Unix-ts lower bound on ts_start.",
    )


class _RecentFailuresArgs(BaseModel):
    window_seconds: float = Field(
        gt=0,
        description="Look back this many seconds from now (e.g. 300 for last 5 minutes).",
    )
    limit: int = Field(default=100, ge=1, le=1000)


class _TopErrorsArgs(BaseModel):
    limit: int = Field(default=20, ge=1, le=1000)


class _TopConsumersArgs(BaseModel):
    limit: int = Field(default=20, ge=1, le=1000)


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
        name="audit_get_call_by_id",
        description=(
            "Return the full audit row for a specific call by primary key. "
            "Useful for deep-dive debugging when you've found a row id via "
            "audit_get_recent_calls or audit_search_arguments."
        ),
        inputSchema=_GetByIdArgs.model_json_schema(),
    ),
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
        name="audit_get_calls_by_client",
        description=(
            "Return the most recent N calls whose client_info matches a SQL LIKE "
            "pattern. Drill-down companion to audit_get_top_consumers — once "
            "you've identified a noisy IP or UA, this shows its actual history. "
            'Pattern uses % as wildcard, e.g. "%10.0.0.5%".'
        ),
        inputSchema=_ByClientArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_get_failed_calls",
        description="Return the most recent N failed tool calls (success = false only).",
        inputSchema=_FailedArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_get_recent_failures",
        description=(
            "Return failed calls whose ts_start is within the last "
            "`window_seconds`. Pairs nicely with dashboards — 'show me "
            "everything that broke in the last 5 minutes' is one call."
        ),
        inputSchema=_RecentFailuresArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_count",
        description=(
            "Return COUNT(*) of audit rows matching the optional filters: "
            "tool_name (equality), success (true/false), since_ts (lower "
            "bound on ts_start). Filters AND together; omit to leave open."
        ),
        inputSchema=_CountArgs.model_json_schema(),
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
            "Full-text LIKE search over call payloads. Default scope is the "
            'arguments JSON; set include_response=true to search both. Use SQL '
            'wildcard syntax — e.g. pattern="%example.com%". Optionally narrow '
            "by tool_name."
        ),
        inputSchema=_SearchArgsModel.model_json_schema(),
    ),
    types.Tool(
        name="audit_export_csv",
        description=(
            "Export up to `limit` audit rows as CSV text with a header row, "
            "optionally filtered to rows with id > since_id. Fields are: "
            "id, ts_start, ts_end, duration_ms, tool_name, success, error, "
            "arguments (JSON), response (JSON), client_info, downstream_target."
        ),
        inputSchema=_ExportArgs.model_json_schema(),
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
        name="audit_get_slowest_calls",
        description=(
            "Return the N slowest tool calls overall (longest duration first). "
            "Useful for finding pathological invocations or downstream regressions."
        ),
        inputSchema=_SlowestArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_get_top_errors",
        description=(
            "Group failed calls by (tool_name, error) and return the most "
            "frequent. Each row reports occurrences and last_seen (Unix ts)."
        ),
        inputSchema=_TopErrorsArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_get_top_consumers",
        description=(
            "Group calls by client_info (IP/UA snippet) and return the top N "
            "callers with per-client call count, error count, error rate, and "
            "last-seen timestamp. Useful for spotting noisy clients."
        ),
        inputSchema=_TopConsumersArgs.model_json_schema(),
    ),
    types.Tool(
        name="audit_vacuum",
        description=(
            "Run SQLite VACUUM to reclaim disk space after a large purge. "
            "Briefly blocks writers; reads continue. Returns the size before "
            "and after as JSON."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
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

    Handlers are registered using the SDK's decorator API (mcp 1.27+):
      @srv.list_tools()  →  func() -> list[Tool]
      @srv.call_tool()   →  func(name, arguments) -> CallToolResult | list[ContentBlock]
    """
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
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
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

    return srv


# ----------------------------------------------------------- audit tool dispatch


def _run_audit_tool(
    store: AuditStore, name: str, arguments: dict[str, Any]
) -> list[types.ContentBlock]:
    if name == "audit_get_call_by_id":
        a_id = _GetByIdArgs.model_validate(arguments)
        row = store.get_by_id(a_id.call_id)
        if row is None:
            return [types.TextContent(type="text", text=f"No audit row with id={a_id.call_id}")]
        return [types.TextContent(type="text", text=json.dumps(row, indent=2, default=str))]
    if name == "audit_get_recent_calls":
        a = _RecentArgs.model_validate(arguments)
        out: Any = store.recent(limit=a.limit)
    elif name == "audit_get_calls_by_tool":
        a2 = _ByToolArgs.model_validate(arguments)
        out = store.by_tool(tool_name=a2.tool_name, limit=a2.limit)
    elif name == "audit_get_calls_by_client":
        a_bc = _ByClientArgs.model_validate(arguments)
        out = store.by_client(client_pattern=a_bc.client_pattern, limit=a_bc.limit)
    elif name == "audit_get_failed_calls":
        a3 = _FailedArgs.model_validate(arguments)
        out = store.failed(limit=a3.limit)
    elif name == "audit_get_recent_failures":
        a_rf = _RecentFailuresArgs.model_validate(arguments)
        out = store.recent_failures(window_seconds=a_rf.window_seconds, limit=a_rf.limit)
    elif name == "audit_count":
        a_cnt = _CountArgs.model_validate(arguments)
        n = store.count(
            tool_name=a_cnt.tool_name,
            success=a_cnt.success,
            since_ts=a_cnt.since_ts,
        )
        return [types.TextContent(type="text", text=json.dumps({"count": n}))]
    elif name == "audit_get_call_stats":
        out = store.stats()
    elif name == "audit_get_calls_in_range":
        a4 = _RangeArgs.model_validate(arguments)
        out = store.in_range(start_ts=a4.start_ts, end_ts=a4.end_ts, limit=a4.limit)
    elif name == "audit_search_arguments":
        a5 = _SearchArgsModel.model_validate(arguments)
        out = store.search_arguments(
            pattern=a5.pattern,
            tool_name=a5.tool_name,
            include_response=a5.include_response,
            limit=a5.limit,
        )
    elif name == "audit_get_slowest_calls":
        a_slow = _SlowestArgs.model_validate(arguments)
        out = store.slowest(limit=a_slow.limit)
    elif name == "audit_get_top_errors":
        a_top = _TopErrorsArgs.model_validate(arguments)
        out = store.top_errors(limit=a_top.limit)
    elif name == "audit_get_top_consumers":
        a_tc = _TopConsumersArgs.model_validate(arguments)
        out = store.top_consumers(limit=a_tc.limit)
    elif name == "audit_vacuum":
        before = store.db_size_bytes()
        store.vacuum()
        after = store.db_size_bytes()
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "size_bytes_before": before,
                        "size_bytes_after": after,
                        "reclaimed_bytes": max(0, before - after),
                    }
                ),
            )
        ]
    elif name == "audit_export_jsonl":
        a6 = _ExportArgs.model_validate(arguments)
        rows = store.export(limit=a6.limit, since_id=a6.since_id)
        lines = "\n".join(json.dumps(r, default=str) for r in rows)
        return [types.TextContent(type="text", text=lines or "(no records)")]
    elif name == "audit_export_csv":
        a_csv = _ExportArgs.model_validate(arguments)
        csv_text = store.export_csv(limit=a_csv.limit, since_id=a_csv.since_id)
        return [types.TextContent(type="text", text=csv_text or "(no records)")]
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
            isError=is_error,
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
                client_info=client_info_var.get(),
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
