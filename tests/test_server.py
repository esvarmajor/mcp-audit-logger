"""Integration tests for the MCP server layer.

These tests use the MCP SDK's in-process testing helpers to exercise the full
`build_server` → handler dispatch path without any network I/O.

All tests run against a real in-memory AuditStore (backed by a tmp SQLite file)
so we verify the persistence side-effect at the same time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mcp.types as types
import pytest
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_audit_logger.server import AUDIT_TOOL_NAMES, build_server
from mcp_audit_logger.storage import AuditStore

# ---------------------------------------------------------- helpers / fixtures


@pytest.fixture()
def store(tmp_path: Path) -> AuditStore:
    return AuditStore(tmp_path / "audit.db")


@pytest.fixture()
def server(store: AuditStore) -> Server:
    """A server with no downstream proxy — only audit_* tools."""
    return build_server(proxy=None, store=store)


@pytest.fixture()
def server_with_downstream(store: AuditStore, downstream_server: Server) -> Server:
    """Proxy server wired to a real in-process downstream."""
    # We expose a pre-built session via a monkey-patched proxy stub.
    return build_server(proxy=_StubProxy(downstream_server, store), store=store)


async def _call(server: Server, tool: str, args: dict[str, Any]) -> types.CallToolResult:
    """Run a tools/call against server using in-process transport."""
    async with create_connected_server_and_client_session(server) as client:
        return await client.call_tool(tool, args)


async def _list(server: Server) -> list[types.Tool]:
    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()
        return result.tools


# ----------------------------------------- Stub proxy (no real network needed)


class _StubProxy:
    """A fake DownstreamProxy that delegates to an in-process MCP server."""

    target_label = "stub:in-process"

    def __init__(self, srv: Server, store: AuditStore) -> None:
        self._srv = srv
        self._store = store
        self._client: Any = None
        self._stack: Any = None

    async def list_tools(self) -> list[types.Tool]:
        if self._client is None:
            raise RuntimeError("StubProxy not started")
        result = await self._client.list_tools()
        return result.tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> Any:
        if self._client is None:
            raise RuntimeError("StubProxy not started")
        return await self._client.call_tool(name, arguments or {})


# ------------------------------------------------------------------ unit tests


@pytest.mark.asyncio
async def test_list_tools_returns_audit_tools(server: Server) -> None:
    tools = await _list(server)
    names = {t.name for t in tools}
    assert AUDIT_TOOL_NAMES.issubset(names), f"Missing audit tools: {AUDIT_TOOL_NAMES - names}"


@pytest.mark.asyncio
async def test_audit_get_recent_calls_empty(server: Server) -> None:
    result = await _call(server, "audit_get_recent_calls", {})
    assert result.isError is not True
    data = json.loads(result.content[0].text)
    assert data == []


@pytest.mark.asyncio
async def test_audit_get_call_stats_empty(server: Server) -> None:
    result = await _call(server, "audit_get_call_stats", {})
    assert result.isError is not True
    data = json.loads(result.content[0].text)
    assert data == []


@pytest.mark.asyncio
async def test_unknown_tool_no_proxy_returns_error(server: Server) -> None:
    result = await _call(server, "fetch", {"url": "https://example.com"})
    assert result.isError is True
    assert "No downstream server" in result.content[0].text


@pytest.mark.asyncio
async def test_audit_get_recent_calls_with_rows(server: Server, store: AuditStore) -> None:
    import time
    t = time.time()
    store.log_call(
        ts_start=t, ts_end=t + 0.1, tool_name="fetch",
        arguments={"url": "https://x.com"}, response={"ok": True},
        success=True, error=None,
    )
    result = await _call(server, "audit_get_recent_calls", {"limit": 5})
    data = json.loads(result.content[0].text)
    assert len(data) == 1
    assert data[0]["tool_name"] == "fetch"


@pytest.mark.asyncio
async def test_audit_get_failed_calls(server: Server, store: AuditStore) -> None:
    import time
    t = time.time()
    store.log_call(
        ts_start=t, ts_end=t + 0.1, tool_name="fetch",
        arguments={}, response={}, success=True, error=None,
    )
    store.log_call(
        ts_start=t, ts_end=t + 0.2, tool_name="search",
        arguments={}, response={}, success=False, error="timeout",
    )
    result = await _call(server, "audit_get_failed_calls", {})
    data = json.loads(result.content[0].text)
    assert len(data) == 1
    assert data[0]["tool_name"] == "search"


@pytest.mark.asyncio
async def test_audit_get_calls_in_range(server: Server, store: AuditStore) -> None:
    import time
    t = time.time()
    store.log_call(
        ts_start=t - 100, ts_end=t - 99, tool_name="old",
        arguments={}, response={}, success=True, error=None,
    )
    store.log_call(
        ts_start=t, ts_end=t + 0.1, tool_name="recent",
        arguments={}, response={}, success=True, error=None,
    )
    result = await _call(server, "audit_get_calls_in_range", {"start_ts": t - 1, "end_ts": t + 1})
    data = json.loads(result.content[0].text)
    assert len(data) == 1
    assert data[0]["tool_name"] == "recent"


@pytest.mark.asyncio
async def test_audit_search_arguments(server: Server, store: AuditStore) -> None:
    import time
    t = time.time()
    store.log_call(
        ts_start=t, ts_end=t + 0.1, tool_name="fetch",
        arguments={"url": "https://example.com"}, response={}, success=True, error=None,
    )
    store.log_call(
        ts_start=t, ts_end=t + 0.1, tool_name="fetch",
        arguments={"url": "https://other.com"}, response={}, success=True, error=None,
    )
    result = await _call(server, "audit_search_arguments", {"pattern": "%example.com%"})
    data = json.loads(result.content[0].text)
    assert len(data) == 1
    assert data[0]["arguments"]["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_audit_export_jsonl(server: Server, store: AuditStore) -> None:
    import time
    t = time.time()
    for i in range(3):
        store.log_call(
            ts_start=t + i, ts_end=t + i + 0.1, tool_name=f"tool{i}",
            arguments={}, response={}, success=True, error=None,
        )
    result = await _call(server, "audit_export_jsonl", {"limit": 2, "since_id": 0})
    lines = [ln for ln in result.content[0].text.strip().split("\n") if ln]
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert "tool_name" in row


@pytest.mark.asyncio
async def test_audit_purge_dry_run(server: Server, store: AuditStore) -> None:
    import time
    t = time.time()
    store.log_call(
        ts_start=t - 1000, ts_end=t - 999, tool_name="old",
        arguments={}, response={}, success=True, error=None,
    )
    result = await _call(server, "audit_purge", {"before_ts": t, "dry_run": True})
    out = json.loads(result.content[0].text)
    assert out["rows_affected"] == 1
    # dry_run=True — row should NOT be deleted
    assert len(store.recent(10)) == 1


@pytest.mark.asyncio
async def test_audit_purge_real(server: Server, store: AuditStore) -> None:
    import time
    t = time.time()
    store.log_call(
        ts_start=t - 1000, ts_end=t - 999, tool_name="old",
        arguments={}, response={}, success=True, error=None,
    )
    result = await _call(server, "audit_purge", {"before_ts": t, "dry_run": False})
    out = json.loads(result.content[0].text)
    assert out["rows_affected"] == 1
    assert len(store.recent(10)) == 0


@pytest.mark.asyncio
async def test_invalid_arguments_returns_error(server: Server) -> None:
    # limit must be ge=1; the SDK's jsonschema validation fires first
    result = await _call(server, "audit_get_recent_calls", {"limit": -5})
    assert result.isError is True
    assert "validation error" in result.content[0].text.lower()
