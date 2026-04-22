"""Proxy connection to a downstream MCP server.

Holds a long-lived `ClientSession` pointed at whatever MCP server we're
sitting in front of. The main server (`server.py`) asks this proxy to list
tools and to forward calls; we don't know or care what those tools actually
do — we just pipe them through.

Supported downstream transports:

    * stdio  — we spawn the child process and talk over its stdin/stdout.
    * Streamable HTTP — we open a long-lived HTTP session to the server URL.

SSE is deliberately **not** supported; it's deprecated in the 2025-03-26
spec revision, and this project targets the newer Streamable HTTP transport.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from .config import DownstreamHttp, DownstreamStdio

log = logging.getLogger(__name__)


class DownstreamProxy:
    """Manages the lifecycle of a ClientSession to a downstream MCP server."""

    def __init__(self, config: DownstreamStdio | DownstreamHttp) -> None:
        self._config = config
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None

    @property
    def target_label(self) -> str:
        """A short identifier used in audit log rows and debug output."""
        if isinstance(self._config, DownstreamHttp):
            return f"http:{self._config.url}"
        args = " ".join(self._config.args)
        return f"stdio:{self._config.command} {args}".strip()

    async def start(self) -> None:
        """Open the transport and run MCP `initialize` on the session."""
        if self._session is not None:
            return
        self._stack = AsyncExitStack()
        try:
            if isinstance(self._config, DownstreamHttp):
                # streamable_http_client does NOT accept a `headers=` kwarg directly —
                # custom headers must be attached to a user-provided httpx.AsyncClient
                # (which the SDK keeps alive for the life of the session).
                http_client: httpx.AsyncClient | None = None
                if self._config.headers:
                    http_client = await self._stack.enter_async_context(
                        httpx.AsyncClient(headers=self._config.headers)
                    )
                ctx = streamable_http_client(self._config.url, http_client=http_client)
                # streamable_http_client yields a 3-tuple (read, write, get_session_id)
                transport = await self._stack.enter_async_context(ctx)
                read, write = transport[0], transport[1]
            else:
                params = StdioServerParameters(
                    command=self._config.command,
                    args=self._config.args,
                    env=dict(self._config.env) if self._config.env else None,
                    cwd=self._config.cwd,
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))

            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
            log.info("downstream.connected", extra={"target": self.target_label})
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Tear down the session and transport. Idempotent."""
        stack = self._stack
        self._stack = None
        self._session = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                log.exception("downstream.stop_failed")

    # -------------------------------------------------------------- proxy verbs

    async def list_tools(self) -> list[Any]:
        assert self._session is not None, "DownstreamProxy not started"
        result = await self._session.list_tools()
        return list(result.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> Any:
        assert self._session is not None, "DownstreamProxy not started"
        return await self._session.call_tool(name, arguments or {})
