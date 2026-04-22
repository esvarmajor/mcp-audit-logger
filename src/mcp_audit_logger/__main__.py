"""CLI entry point for `mcp-audit-logger`.

Boots the Streamable HTTP transport, wires up the proxy and audit store,
and hands everything to uvicorn. Kill with Ctrl-C.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from .config import Config, load_config
from .logging_config import configure_logging
from .proxy import DownstreamProxy
from .server import build_server
from .storage import AuditStore

log = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mcp-audit-logger",
        description=(
            "A transparent MCP middleware proxy that audits every tool call "
            "to a local SQLite database."
        ),
    )
    p.add_argument("--config", help="Path to JSON config file (overrides env).")
    p.add_argument("--host", help="Host to bind (default 127.0.0.1).")
    p.add_argument("--port", type=int, help="Port to bind (default 8765).")
    p.add_argument("--db-path", help="SQLite DB file (default ./audit.db).")
    p.add_argument("--mount-path", help="URL path to mount MCP at (default /mcp).")
    p.add_argument(
        "--log-level",
        help="Log level (DEBUG, INFO, WARNING, ERROR). Default INFO.",
    )
    return p.parse_args()


def _apply_cli_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.db_path:
        cfg.db_path = Path(args.db_path)
    if args.mount_path:
        cfg.mount_path = args.mount_path
    if args.log_level:
        cfg.log_level = args.log_level
    return cfg


async def _run(cfg: Config) -> None:
    store = AuditStore(cfg.db_path, max_payload_bytes=cfg.max_payload_bytes)

    proxy: DownstreamProxy | None = None
    if cfg.downstream is not None:
        proxy = DownstreamProxy(cfg.downstream)
        await proxy.start()
    else:
        log.warning(
            "downstream.not_configured",
            extra={
                "hint": (
                    "No downstream MCP server configured — only audit_* tools will "
                    "be available. Set AUDIT_DOWNSTREAM_COMMAND/AUDIT_DOWNSTREAM_URL "
                    "or add a `downstream` block to your config file."
                )
            },
        )

    srv = build_server(proxy=proxy, store=store)

    session_mgr = StreamableHTTPSessionManager(
        app=srv,
        event_store=None,
        json_response=False,
        stateless=False,
    )

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await session_mgr.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_mgr.run():
            log.info(
                "server.started",
                extra={"host": cfg.host, "port": cfg.port, "path": cfg.mount_path},
            )
            try:
                yield
            finally:
                if proxy is not None:
                    await proxy.stop()
                log.info("server.stopped")

    app = Starlette(
        routes=[Mount(cfg.mount_path, app=handle_mcp)],
        lifespan=lifespan,
    )

    uvicorn_cfg = uvicorn.Config(
        app,
        host=cfg.host,
        port=cfg.port,
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_cfg)
    await server.serve()


def main() -> None:
    args = _parse_args()
    cfg = load_config(args.config)
    cfg = _apply_cli_overrides(cfg, args)
    configure_logging(cfg.log_level)
    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
