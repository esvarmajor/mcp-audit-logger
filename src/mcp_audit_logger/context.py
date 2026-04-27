"""Per-request client info propagation for the audit log.

The MCP SDK's `Server.call_tool` handler doesn't see the underlying ASGI
scope, so we can't read it directly to populate `client_info` on each row.
Instead, we set a `ContextVar` in the outer ASGI wrapper before handing off
to the session manager, and read it back in the call-tool handler. ContextVar
values are task-local, so concurrent requests don't bleed into each other.
"""

from __future__ import annotations

import contextvars
import json
from typing import Any

from starlette.types import Scope

# Public read API. The default of None means "no info captured" — calls made
# from a non-HTTP context (e.g. unit tests) will continue to log NULL into
# audit_calls.client_info, matching pre-existing behavior.
client_info_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_audit_client_info", default=None
)


def build_client_info_from_scope(scope: Scope, *, has_token: bool) -> str | None:
    """Build a compact JSON summary of the request origin, or None if empty.

    Captures: client IP, truncated user-agent, and a presence marker for the
    bearer token (never the token itself). Truncating the UA keeps the column
    bounded; 200 chars is enough for any real-world MCP client identifier.
    """
    info: dict[str, Any] = {}

    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        info["ip"] = str(client[0])

    headers = scope.get("headers") or []
    for k, v in headers:
        if k.lower() == b"user-agent":
            info["ua"] = v.decode("utf-8", errors="replace")[:200]
            break

    if has_token:
        # We never store the token. A presence marker is enough for the
        # operator to know which request stream the call came in on.
        info["auth"] = "bearer"

    if not info:
        return None
    return json.dumps(info, separators=(",", ":"))
