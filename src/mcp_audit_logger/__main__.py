"""CLI entry point for `mcp-audit-logger`.

Boots the Streamable HTTP transport, wires up the proxy and audit store,
and hands everything to uvicorn. Kill with Ctrl-C.

Transport flow:
  client → Starlette (our app) → StreamableHTTPSessionManager → Server
                                                                   └→ DownstreamProxy → real server
                                                                   └→ AuditStore (SQLite)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from . import __version__
from .config import Config, load_config
from .context import build_client_info_from_scope, client_info_var
from .logging_config import configure_logging
from .metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE
from .metrics import render as render_metrics
from .proxy import DownstreamProxy
from .server import build_server
from .storage import AuditStore

log = logging.getLogger(__name__)


def _auth_wrap(
    expected_token: str | None,
    inner: Callable[[Scope, Receive, Send], Any],
) -> Callable[[Scope, Receive, Send], Any]:
    """Return an ASGI callable that gates `inner` behind bearer-token auth.

    When `expected_token` is None, returns `inner` unchanged (auth disabled).
    When set, any request without a matching Authorization header gets a 401.
    """
    if expected_token is None:
        return inner

    async def _handler(scope: Scope, receive: Receive, send: Send) -> None:
        # ASGI header keys are bytes; decode before building the lookup dict.
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if headers.get("authorization") != expected_token:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [[b"content-type", b"text/plain; charset=utf-8"]],
            })
            await send({
                "type": "http.response.body",
                "body": b"Unauthorized",
                "more_body": False,
            })
            return
        await inner(scope, receive, send)

    return _handler


def _capture_client_info(
    inner: Callable[[Scope, Receive, Send], Any],
    *,
    has_token: bool,
) -> Callable[[Scope, Receive, Send], Any]:
    """Wrap `inner` with a layer that sets `client_info_var` for the request.

    The ContextVar is reset in a `finally` so the value never leaks into a
    sibling task. Non-HTTP scopes (e.g. lifespan messages) are passed through
    untouched.
    """

    async def _wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await inner(scope, receive, send)
            return
        info = build_client_info_from_scope(scope, has_token=has_token)
        token = client_info_var.set(info)
        try:
            await inner(scope, receive, send)
        finally:
            client_info_var.reset(token)

    return _wrapped


def _build_health_payload(
    store: AuditStore, proxy: DownstreamProxy | None
) -> tuple[int, dict[str, Any]]:
    """Compute the /healthz status code and JSON body."""
    db_ok = True
    try:
        store.db_size_bytes()
    except Exception:
        db_ok = False

    # not_configured → None; configured → True/False based on session presence.
    downstream_ok: bool | None = (
        None if proxy is None else proxy._session is not None
    )

    ready = db_ok and (downstream_ok is not False)
    body = {
        "status": "ok" if ready else "degraded",
        "db_writable": db_ok,
        "downstream": (
            "connected"
            if downstream_ok is True
            else "disconnected"
            if downstream_ok is False
            else "not_configured"
        ),
        "version": __version__,
    }
    return (200 if ready else 503), body


def _make_health_endpoint(
    store: AuditStore, proxy: DownstreamProxy | None
) -> Callable[[Request], Any]:
    """Starlette endpoint for /healthz — never gated by auth."""

    async def _endpoint(_request: Request) -> Response:
        status, body = _build_health_payload(store, proxy)
        return JSONResponse(body, status_code=status)

    return _endpoint


def _make_metrics_endpoint(
    store: AuditStore, *, expected_token: str | None
) -> Callable[[Request], Any]:
    """Starlette endpoint for /metrics. Gated by bearer if expected_token is set."""

    async def _endpoint(request: Request) -> Response:
        if expected_token is not None and request.headers.get("authorization") != expected_token:
            return PlainTextResponse("Unauthorized", status_code=401)
        return Response(render_metrics(store), media_type=METRICS_CONTENT_TYPE)

    return _endpoint


# ---------------------------------------------------------------------- CLI


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mcp-audit-logger",
        description=(
            "A transparent MCP middleware proxy that audits every tool call "
            "to a local SQLite database."
        ),
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"mcp-audit-logger {__version__}",
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


# ---------------------------------------------------------------------- server


async def _run(cfg: Config) -> None:
    store = AuditStore(cfg.db_path, max_payload_bytes=cfg.max_payload_bytes)

    if cfg.downstream is None:
        log.warning(
            "downstream.not_configured",
            extra={
                "hint": (
                    "No downstream server configured — only audit_* tools are available. "
                    "Set AUDIT_DOWNSTREAM_COMMAND or AUDIT_DOWNSTREAM_URL."
                )
            },
        )

    proxy: DownstreamProxy | None = (
        DownstreamProxy(cfg.downstream) if cfg.downstream is not None else None
    )
    srv = build_server(proxy=proxy, store=store)

    session_mgr = StreamableHTTPSessionManager(
        app=srv,
        event_store=None,
        json_response=False,
        stateless=False,
    )

    expected_token: str | None = (
        f"Bearer {cfg.http_token}" if cfg.http_token else None
    )
    if expected_token:
        log.info("auth.bearer_token_enabled")

    handle_mcp = _auth_wrap(
        expected_token,
        _capture_client_info(
            session_mgr.handle_request, has_token=expected_token is not None
        ),
    )

    async def _retention_loop(store: AuditStore, retention_days: int) -> None:
        """Delete rows older than `retention_days` once per hour, forever."""
        interval = 3600.0
        cutoff_seconds = retention_days * 86400.0
        while True:
            await anyio.sleep(interval)
            try:
                deleted = store.purge(before_ts=time.time() - cutoff_seconds, dry_run=False)
                if deleted:
                    log.info(
                        "retention.purged",
                        extra={"rows": deleted, "retention_days": retention_days},
                    )
            except Exception:
                log.exception("retention.purge_failed")

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        # Start the proxy and session manager in the same anyio task group so that
        # stop() later exits the stdio_client cancel scope from the same task that
        # entered it. Starting proxy outside session_mgr.run() and stopping it inside
        # causes a "cancel scope in different task" RuntimeError in anyio.
        async with session_mgr.run():
            if proxy is not None:
                await proxy.start()
                log.info("downstream.ready", extra={"target": proxy.target_label})
            if cfg.retention_days:
                log.info(
                    "retention.enabled",
                    extra={"retention_days": cfg.retention_days},
                )
            log.info(
                "server.started",
                extra={"host": cfg.host, "port": cfg.port, "path": cfg.mount_path},
            )
            try:
                if cfg.retention_days:
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(_retention_loop, store, cfg.retention_days)
                        yield
                        tg.cancel_scope.cancel()
                else:
                    yield
            finally:
                if proxy is not None:
                    await proxy.stop()
                log.info("server.stopped")

    # Use Starlette Route (not Mount) for /healthz and /metrics so bare
    # paths don't 307-redirect to a trailing-slash variant.
    routes: list[Any] = [
        Mount(cfg.mount_path, app=handle_mcp),
        Route(
            cfg.health_path,
            _make_health_endpoint(store, proxy),
            methods=["GET", "HEAD"],
        ),
    ]
    if cfg.enable_metrics:
        routes.append(
            Route(
                cfg.metrics_path,
                _make_metrics_endpoint(store, expected_token=expected_token),
                methods=["GET", "HEAD"],
            )
        )
        log.info("metrics.enabled", extra={"path": cfg.metrics_path})

    app = Starlette(routes=routes, lifespan=lifespan)

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
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(cfg))


if __name__ == "__main__":
    main()
