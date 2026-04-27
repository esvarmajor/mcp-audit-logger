# Gameplan — follow-up work

This doc is the running handoff. What's **done** is listed for context;
everything under "Open" is fair game next.

---

## Done

### Foundation (0.1.0)

- Core proxy for stdio + Streamable HTTP downstreams (`proxy.py`).
- SQLite audit store with WAL mode and payload truncation (`storage.py`).
- Streamable HTTP transport via `StreamableHTTPSessionManager` with a
  Starlette lifespan that shares the anyio task-group scope.
- Eight `audit_*` query tools.
- Optional bearer token auth on the HTTP endpoint (`AUDIT_HTTP_TOKEN`).
- Config loader with CLI > env > file > defaults precedence.
- Structured JSON logging on stderr.
- Time-based retention (`AUDIT_RETENTION_DAYS`, hourly purge loop).
- CI matrix (Python 3.10/3.11/3.12 × ruff + mypy + pytest) and PyPI
  trusted-publisher release workflow.

### 0.2.0

- **`audit_get_slowest_calls`** — N slowest calls overall.
- **`audit_get_top_errors`** — `(tool, error)` group counts.
- **`audit_get_top_consumers`** — per-client call/error counts.
- **`audit_get_call_by_id`** — single-row lookup.
- **`audit_vacuum`** — SQLite VACUUM with size-before/after report.
- **p50 / p95** in `audit_get_call_stats`.
- **`audit_search_arguments`** gained `tool_name` filter and
  `include_response` flag.
- **Prometheus `/metrics`** endpoint, opt-in (`AUDIT_ENABLE_METRICS`).
  No `prometheus_client` dep — synthesized from `store.stats()` per scrape.
- **Always-on `/healthz`** for k8s-style probes. Reports DB writability +
  downstream session status; never gated by auth.
- **`client_info` populated per row** via a `ContextVar` set in the outer
  ASGI wrapper. Captures IP, truncated UA, and a presence marker for the
  bearer token (never the token itself).
- **`scripts/smoke.py`** — end-to-end smoke test that boots the proxy and
  round-trips an `audit_get_recent_calls` over real Streamable HTTP.
- **HTTP downstream header propagation** unit tests
  (`tests/test_proxy.py`).
- **`--version`** CLI flag.
- **CHANGELOG.md** tracking the 0.2.0 release.
- **Makefile** with the common dev targets (lint/test/smoke/run).

101 tests passing.

---

## Open

### 1. Downstream reconnection / supervision

Still the most architecturally interesting open item. Design doc at
[`docs/reconnection-design.md`](docs/reconnection-design.md) is current
and covers the anyio cancel-scope pitfall, supervisor task layout,
backoff schedule, and call-timeout behavior during reconnect. The
design has been thought through; what remains is the careful
implementation. Touches the most fragile part of the code — give it a
worktree and a cup of coffee.

### 2. Verify the Streamable HTTP downstream path against a live server

The unit-test half landed in 0.2.0 (`tests/test_proxy.py` mocks the SDK
and asserts header propagation). Still missing: a live round-trip
against a real Streamable HTTP MCP server (or a second
`mcp-audit-logger` instance running with no downstream as a stub).

Adding this to `scripts/smoke.py` behind a `--http-downstream URL`
flag would be the cleanest way.

### 3. Size-based DB rotation

Time-based retention shipped in 0.1.0, and `audit_vacuum` shipped in
0.2.0. Size-based rotation (`audit.db` → `audit.db.YYYYMMDDHHMM` when
it crosses N MB, then re-create) is still on the table but rarely
needed in practice — most operators are fine with retention + vacuum.
Punt unless someone files an issue.

### 4. Health-ping for downstream session

`/healthz` reports `proxy._session is not None`, which catches process
death but not silent TCP drops where calls happen infrequently. The
reconnection design (item 1) has a periodic ping built in; defer until
that lands.

---

## Explicitly out of scope

- Resources / prompts pass-through. (Core brief was tool-call auditing.)
- Multi-downstream fan-out. (One logger, one downstream — keep it simple.)
- Non-SQLite backends. (Whole point was zero external deps.)
- A web UI for browsing `audit.db`. (The `audit_*` MCP tools are the UI.)
