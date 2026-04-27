"""Unit tests for DownstreamProxy.

We don't open a real HTTP or stdio session — instead we monkey-patch
``streamable_http_client`` and ``ClientSession`` so we can observe what
the proxy hands to the SDK. The interesting question is: when a
``DownstreamHttp`` config carries custom headers, do they end up on the
``httpx.AsyncClient`` that the SDK uses for every request?
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx
import pytest

import mcp_audit_logger.proxy as proxy_mod
from mcp_audit_logger.config import DownstreamHttp
from mcp_audit_logger.proxy import DownstreamProxy


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace streamable_http_client and ClientSession with stubs.

    Returns a dict that the test can inspect after calling start():
        url            — what was passed to streamable_http_client
        http_client    — the httpx.AsyncClient (or None) the proxy built
        initialized    — True once session.initialize() ran
    """
    captured: dict[str, Any] = {"initialized": False}

    @contextlib.asynccontextmanager
    async def fake_streamable_http_client(url: str, *, http_client: Any = None):
        captured["url"] = url
        captured["http_client"] = http_client

        async def _recv() -> None: ...
        async def _send(_msg: object) -> None: ...

        yield (_recv, _send, lambda: "fake-session-id")

    class _FakeSession:
        def __init__(self, read: Any, write: Any) -> None:
            self._read = read
            self._write = write

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def initialize(self) -> None:
            captured["initialized"] = True

    monkeypatch.setattr(proxy_mod, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(proxy_mod, "ClientSession", _FakeSession)
    return captured


@pytest.mark.asyncio
async def test_http_downstream_propagates_headers_to_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_fake_sdk(monkeypatch)
    cfg = DownstreamHttp(
        url="https://example.com/mcp",
        headers={"X-Api-Key": "tok-abc-123", "X-Tenant": "acme"},
    )
    proxy = DownstreamProxy(cfg)
    await proxy.start()
    try:
        assert captured["url"] == "https://example.com/mcp"
        http_client = captured["http_client"]
        assert isinstance(http_client, httpx.AsyncClient)
        # httpx normalizes header keys to lowercase for lookup but preserves case in __iter__.
        assert http_client.headers["x-api-key"] == "tok-abc-123"
        assert http_client.headers["x-tenant"] == "acme"
        assert captured["initialized"] is True
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_http_downstream_skips_httpx_client_when_no_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_fake_sdk(monkeypatch)
    cfg = DownstreamHttp(url="https://example.com/mcp", headers={})
    proxy = DownstreamProxy(cfg)
    await proxy.start()
    try:
        # No headers configured → no httpx.AsyncClient is built; SDK gets None.
        assert captured["http_client"] is None
        assert captured["initialized"] is True
    finally:
        await proxy.stop()


def test_target_label_for_http_downstream() -> None:
    cfg = DownstreamHttp(url="https://example.com/mcp", headers={})
    assert DownstreamProxy(cfg).target_label == "http:https://example.com/mcp"


@pytest.mark.asyncio
async def test_stop_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(monkeypatch)
    cfg = DownstreamHttp(url="https://example.com/mcp", headers={})
    proxy = DownstreamProxy(cfg)
    await proxy.start()
    await proxy.stop()
    # Calling stop() again should not raise.
    await proxy.stop()
