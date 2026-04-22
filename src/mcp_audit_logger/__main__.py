"""CLI entry point for `mcp-audit-logger`.

Boots the proxy, builds the MCP server, wraps it in a Starlette ASGI app via
`server.streamable_http_app()`, optionally adds bearer auth middleware, then
hands off to uvicorn. Proxy start/stop is managed around the uvicorn lifecycle.

Kill with Ctrl-C.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any

import uvicorn
from starlette.requests import Request
from starlette.responses import Response

from .config import Config, load_config
from .logging_config import configure_logging
from .proxy import DownstreamProxy
from .server import build_server
from .storage import AuditStore

log = logging.getLogger(__name__)


# --------------------------------------------------------------- bearer auth


class BearerAuthMiddleware:
    """Thin ASGI middleware that enforces a static bearer token on every request."""

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = {k.lower(): v.decode() for k, v in scope.get("headers", [])}
            if headers.get("authorization") != self._expected:
                response = Response("Unauthorized", status_code=401, media_type="text/plain")
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


# --------------------------------------------------------------- CLI


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
    p.add_argument("--mount-path", help="URL path for the MCP endpoint (default /mcp).")
    p.add_argument("--log-level", help="Log level: DEBUG, INFO, WARNING, ERROR (default INFO).")
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


# --------------------------------------------------------------- main loop


async def _run(cfg: Config) -> None:
    store = AuditStore(cfg.db_path, max_payload_bytes=cfg.max_payload_bytes)

    proxy: DownstreamProxy | None = None
    if cfg.downstream is not None:
        proxy = DownstreamProxy(cfg.downstream)
        await proxy.start()
        log.info("downstream.ready", extra={"target": proxy.target_label})
    else:
        log.warning(
            "downstream.not_configured",
            extra={
                "hint": (
                    "No downstream server configured — only audit_* tools are available. "
                    "Set AUDIT_DOWNSTREAM_COMMAND or AUDIT_DOWNSTREAM_URL."
                )
            },
        )

    try:
        srv = build_server(proxy=proxy, store=store)

        # streamable_http_app creates a Starlette app whose lifespan manages the
        # StreamableHTTPSessionManager (starts/stops the anyio task group).
        # The `host` parameter enables DNS-rebinding protection on localhost.
        app = srv.streamable_http_app(
            streamable_http_path=cfg.mount_path,
            host=cfg.host,
        )

        if cfg.http_token:
            app.add_middleware(BearerAuthMiddleware, token=cfg.http_token)
            log.info("auth.bearer_token_enabled")

        log.info("server.starting", extra={"host": cfg.host, "port": cfg.port, "path": cfg.mount_path})

        uvicorn_cfg = uvicorn.Config(
            app,
            host=cfg.host,
            port=cfg.port,
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(uvicorn_cfg)
        await server.serve()

    finally:
        if proxy is not None:
            await proxy.stop()
            log.info("downstream.stopped")


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
