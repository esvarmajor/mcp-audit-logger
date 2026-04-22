# Gameplan — follow-up work for Sonnet

This doc is the handoff from the Opus 4.7 foundation pass to a Sonnet session
that will finish off the open-source polish. The initial commit covers:

- Core proxy (stdio + Streamable HTTP downstream)
- Structured JSON logging on stderr
- SQLite audit store with WAL mode and payload truncation
- Four `audit_*` MCP tools exposed on the proxy's own Streamable HTTP endpoint
- `pyproject.toml`, README, LICENSE, `.gitignore`, example configs
- 8 passing unit tests for the storage layer

Everything below is **not yet done** — that's the queue.

---

## 1. Runtime verification against a real downstream (blocking)

The storage tests pass, but I never got a full end-to-end run because the dev
machine only has Python 3.9 and the MCP SDK requires 3.10+.

**Do first:**

1. Install Python 3.11 (`brew install python@3.11`).
2. `python3.11 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'`.
3. `pip install mcp-server-fetch` (or pick any reference server on PyPI).
4. `mcp-audit-logger --config ./examples/config.json --log-level DEBUG` and
   verify the `server.started` line appears.
5. From a second terminal, connect with the MCP Inspector
   (`npx @modelcontextprotocol/inspector`) pointed at
   `http://127.0.0.1:8765/mcp` and:
   - Confirm `tools/list` shows both the downstream tools and `audit_*`.
   - Call a downstream tool (e.g. `fetch`) and then call
     `audit_get_recent_calls` to confirm it shows up.
   - Force an error (bad input) and confirm `audit_get_failed_calls` picks it up.
6. If anything breaks, the most likely culprits are:
   - `StreamableHTTPSessionManager` import path drift between mcp SDK versions.
     Check `mcp/server/streamable_http_manager.py` in the installed SDK and
     adjust the import in `src/mcp_audit_logger/__main__.py` if it moved.
   - `streamablehttp_client` return-tuple shape (2 vs 3 elements). `proxy.py`
     already handles both shapes defensively; extend if needed.

## 2. Integration tests

`tests/test_storage.py` covers persistence. We still need coverage for:

- `DownstreamProxy.start` / `stop` against an in-process stub MCP server.
  Use `mcp.server.fastmcp.FastMCP` with in-memory streams from
  `mcp.shared.memory.create_connected_server_and_client_session` — the SDK
  ships a helper for this.
- `build_server` handler dispatch:
  - `audit_*` name goes to `_run_audit_tool` and returns text content.
  - Non-audit name with a configured proxy goes to `_proxied_call` and writes
    one row.
  - Non-audit name with no proxy raises a clear `ValueError`.
  - Exception in downstream still writes the row (success=False, error set).
- Config loading precedence (CLI > env > file > defaults).
- `JsonFormatter` emits valid JSON with and without `extra={...}`.

## 3. Additional query tools worth adding

The existing four satisfy the original brief; these are natural follow-ons:

- `audit_get_calls_in_range(start_ts, end_ts, limit)` — time-bounded slice.
- `audit_search_arguments(pattern, limit)` — `LIKE` / `json_extract` search
  into `arguments_json`. Useful for "which calls hit URL X".
- `audit_export_jsonl(path, since_id)` — stream-out for offline analysis.
- `audit_purge(before_ts)` — retention, returning `rows_deleted`.

Each needs a Pydantic arg model in `server.py`, a matching method on
`AuditStore`, and README entries.

## 4. Operational polish

- **Retention / rotation.** Either a configurable `AUDIT_RETENTION_DAYS`
  that runs a daily `DELETE WHERE ts_start < ?`, or size-based rotation
  (rename `audit.db` -> `audit.db.YYYYMMDD` at N MB). Implement in
  `storage.py` + schedule from a background task in `__main__._run`.
- **Prometheus metrics endpoint.** Mount `/metrics` on the Starlette app
  emitting `mcp_calls_total{tool,success}` and a histogram for duration.
- **Optional bearer auth.** `AUDIT_HTTP_TOKEN` — reject requests without
  `Authorization: Bearer ...`. Important if the proxy is ever bound to
  anything other than `127.0.0.1`.
- **Graceful shutdown.** Double-check `proxy.stop()` actually joins the stdio
  child; I used `AsyncExitStack.aclose()` which should, but confirm with a
  smoke test.

## 5. CI, release, publishing

- **GitHub Actions.** `.github/workflows/ci.yml`: matrix of Python 3.10/3.11/3.12
  running `ruff check`, `mypy`, `pytest`. Cache pip. Run on push + PR.
- **Release workflow.** Trusted publishing to PyPI on tag push. Bump
  `__version__` + `pyproject.toml` version together (could script it).
- **Docs site.** Optional — a simple `docs/` with mkdocs-material would be
  nice once the tool graph grows beyond four.

## 6. Packaging edge cases

- The current `pyproject.toml` pins `mcp>=1.2.0`. Before release, test against
  the *actual* latest SDK version and raise the floor if anything relies on
  newer API. Lock any MCP spec-rev-sensitive bits (Streamable HTTP path
  conventions, session ID header names, etc.) behind a thin adapter in
  `proxy.py` so future upgrades land in one place.
- Add a smoke script (`scripts/smoke.sh`) that boots the proxy, runs an
  Inspector session against it, and exits non-zero on failure. Great for CI
  confidence.

## 7. Things that are explicitly out of scope

Don't let the review bike-shed pull these in — they're their own projects:

- Resources / prompts pass-through. (The core brief was tool-call auditing.
  Adding resources is easy but unrequested.)
- Multi-downstream fan-out. (One logger, one downstream — keep it simple.)
- Non-SQLite backends. (The whole point was zero external deps.)
- A web UI for browsing `audit.db`. (The `audit_*` MCP tools are the UI.)

## 8. Publishing the repo

The initial commit exists locally at `/Users/ekannan/Downloads/AuditLoggerMCP`.
`gh` is not installed on this machine so I didn't create the GitHub remote.
To push:

```bash
# Option A — install gh and create the repo:
brew install gh
gh auth login
gh repo create esvarmajor/mcp-audit-logger --public --source=. --remote=origin --push

# Option B — create via the website, then:
git remote add origin git@github.com:esvarmajor/mcp-audit-logger.git
git branch -M main
git push -u origin main
```

The README and `pyproject.toml` already point at
`https://github.com/esvarmajor/mcp-audit-logger`; update both if you pick a
different slug.
