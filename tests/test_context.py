"""Tests for client-info propagation via ContextVar."""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from mcp_audit_logger.__main__ import _capture_client_info
from mcp_audit_logger.context import build_client_info_from_scope, client_info_var

# ----------------------------------------------------- builder unit tests


def _scope(
    *,
    client: tuple[str, int] | None = ("10.0.0.5", 54321),
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Scope:
    return {
        "type": "http",
        "method": "POST",
        "client": client,
        "headers": headers or [],
    }


def test_build_captures_ip_and_user_agent() -> None:
    s = _scope(headers=[(b"user-agent", b"claude-desktop/0.1.234")])
    out = build_client_info_from_scope(s, has_token=False)
    assert out is not None
    info = json.loads(out)
    assert info == {"ip": "10.0.0.5", "ua": "claude-desktop/0.1.234"}


def test_build_marks_auth_when_token_set() -> None:
    out = build_client_info_from_scope(_scope(), has_token=True)
    assert out is not None
    assert json.loads(out)["auth"] == "bearer"


def test_build_truncates_long_user_agents() -> None:
    long_ua = b"x" * 5000
    s = _scope(headers=[(b"user-agent", long_ua)])
    info = json.loads(build_client_info_from_scope(s, has_token=False) or "{}")
    assert len(info["ua"]) == 200


def test_build_returns_none_when_no_signal() -> None:
    # No client, no headers, no auth → nothing useful
    assert build_client_info_from_scope(_scope(client=None), has_token=False) is None


def test_build_handles_user_agent_case_insensitive() -> None:
    s = _scope(headers=[(b"User-Agent", b"Funky/1.0")])
    info = json.loads(build_client_info_from_scope(s, has_token=False) or "{}")
    assert info.get("ua") == "Funky/1.0"


# ------------------------------------------------------- ASGI wrap tests


def test_capture_sets_contextvar_for_inner_handler() -> None:
    captured: list[str | None] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        captured.append(client_info_var.get())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = Starlette(routes=[Mount("/x", app=_capture_client_info(inner, has_token=False))])
    client = TestClient(app)
    client.get("/x", headers={"User-Agent": "my-agent/1"})

    assert len(captured) == 1
    assert captured[0] is not None
    info = json.loads(captured[0])
    assert info["ua"] == "my-agent/1"


def test_capture_resets_contextvar_after_request() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        # Sanity: var is set inside the handler
        assert client_info_var.get() is not None
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = Starlette(routes=[Mount("/x", app=_capture_client_info(inner, has_token=False))])
    client = TestClient(app)
    client.get("/x", headers={"User-Agent": "a"})

    # Outside the request, the wrapper should have reset the var.
    assert client_info_var.get() is None


@pytest.mark.asyncio
async def test_capture_passes_through_non_http_scope() -> None:
    """Lifespan scopes should not touch the ContextVar machinery."""
    calls: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(scope["type"])

    wrapped = _capture_client_info(inner, has_token=False)

    async def receive() -> dict[str, str]:
        return {"type": "lifespan.startup"}

    async def send(_msg: dict[str, str]) -> None:
        pass

    await wrapped({"type": "lifespan"}, receive, send)
    assert calls == ["lifespan"]
