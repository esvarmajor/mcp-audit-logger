# Gameplan — follow-up work

This doc is the handoff from the foundation pass. What's **done** is listed for
context; everything under "Open" is free to pick up next.

---

## Done

- Core proxy for stdio + Streamable HTTP downstreams (`proxy.py`).
- SQLite audit store with WAL mode and payload truncation (`storage.py`).
- Streamable HTTP transport wired through `StreamableHTTPSessionManager` with a
  Starlette lifespan that shares the anyio task-group scope across proxy and
  session manager (avoids the "cancel scope in different task" bug).
- 8 `audit_*` query tools: `get_recent_calls`, `get_calls_by_tool`,
  `get_failed_calls`, `get_call_stats`, `get_calls_in_range`,
  `search_arguments`, `export_jsonl`, `purge`.
- Optional bearer token auth on the HTTP endpoint (`AUDIT_HTTP_TOKEN`).
- Config loader with CLI > env > file > defaults precedence.
- Structured JSON logging on stderr.
- 26 passing tests (14 storage + 12 in-process server integration).
- CI workflow matrix: Python 3.10/3.11/3.12 × ruff + mypy + pytest.
- Release workflow: PyPI trusted-publisher on tag push.
- Verified e2e against `mcp-server-fetch` (stdio downstream).

## Open

### 1. Verify the Streamable HTTP downstream path end-to-end

The stdio downstream was smoke-tested against `mcp-server-fetch`. The HTTP
downstream path has **not** been exercised against a real server. The SDK's
`streamable_http_client` does not accept a `headers=` kwarg directly; we now
route custom headers through a user-provided `httpx.AsyncClient` managed by our
`AsyncExitStack`. That code path needs a live test.

**How:** Pick any public Streamable HTTP MCP server (or run a second
`mcp-audit-logger` with no downstream as a stub), set `AUDIT_DOWNSTREAM_URL`
and `AUDIT_DOWNSTREAM_HEADERS`, confirm `tools/list` proxies through and an
audit row lands for a proxied call. Add a unit test that mocks
`streamable_http_client` and asserts the `httpx.AsyncClient` we built carries
the configured headers.

### 2. Downstream reconnection / supervision

If the downstream stdio child dies mid-session, we currently surface an
exception on the next call and never try to re-establish the session. The user
has to restart the proxy.

This is the most architecturally interesting open item — it requires a
supervisor task inside the lifespan's task group that:
- Watches `ClientSession` health (heartbeat ping or failed-call threshold).
- Tears down the current `AsyncExitStack` on failure.
- Re-runs `DownstreamProxy.start()` with backoff.
- Quiesces in-flight `proxy.call_tool` callers during the reconnect window
  (either fail-fast with a "reconnecting" error, or queue with a bounded
  wait).

The anyio cancel-scope pitfall we hit during initial implementation
(see commit `c76f248`) applies here too: whatever task enters the
`stdio_client` context must also exit it. Recommended approach: have a single
supervisor task own `start()`/`stop()` and expose `call_tool` as a method that
awaits a `ready` event — callers never touch the stack directly.

**Good first task for Sonnet.** Needs a design doc before code.

### 3. Retention / rotation

`audit.db` grows unbounded. Two orthogonal options:

- **Time-based retention.** A background task in `__main__._run` that runs
  `store.purge(before_ts=now - retention_seconds, dry_run=False)` every hour.
  New config key: `AUDIT_RETENTION_DAYS`.
- **Size-based rotation.** Rename `audit.db` to `audit.db.YYYYMMDDHHMM`
  when it crosses N MB and re-create. Harder because existing sqlite
  connections would break; probably only worth doing if retention isn't
  enough.

Start with time-based. The `store.purge` implementation already exists.

### 4. Client info capture

`AuditStore.log_call` has a `client_info` column that is always `None` in
practice. We can populate it from the Starlette scope in the auth/handler
layer: client IP (`scope["client"]`), user-agent, and a redacted label for
the bearer token in use (e.g. first 4 + last 4 chars) if auth is enabled. This
requires plumbing the request scope into the `Server` call-tool handler,
which the MCP SDK doesn't expose directly — the clean path is a
`contextvars.ContextVar` set in `handle_mcp` before `session_mgr.handle_request`
and read in `_proxied_call`.

### 5. Tests for auth middleware and config precedence

Straightforward — `__main__.py`'s inline bearer check and `config.load_config`'s
precedence rules aren't under test. Use Starlette's `TestClient` for the
auth middleware; for config, write to `tmp_path` + `monkeypatch.setenv` and
assert the resolved `Config` fields.

### 6. Prometheus `/metrics` endpoint

Optional but cheap once the lifespan is already there. Mount `/metrics` on the
same Starlette app. Counter `mcp_calls_total{tool,success}` and histogram
`mcp_call_duration_seconds{tool}`. Pull from the existing `log.info`
instrumentation points in `_proxied_call`.

### 7. Smoke script

`scripts/smoke.sh` that boots the proxy against a known downstream, does a
`tools/list` + `tools/call` + `audit_get_recent_calls` round-trip, and exits
non-zero on failure. Great for catching SDK-version-drift breakage in CI
before it hits users.

---

## Explicitly out of scope

Don't let the review bike-shed pull these in — they're their own projects:

- Resources / prompts pass-through. (Core brief was tool-call auditing.)
- Multi-downstream fan-out. (One logger, one downstream — keep it simple.)
- Non-SQLite backends. (Whole point was zero external deps.)
- A web UI for browsing `audit.db`. (The `audit_*` MCP tools are the UI.)
