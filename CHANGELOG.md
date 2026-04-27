# Changelog

All notable changes to `mcp-audit-logger` are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `audit_get_calls_by_client` tool — drill-down companion to
  `audit_get_top_consumers`.
- `audit_get_call_by_id` tool — single-row lookup by primary key.
- `audit_export_csv` tool — CSV export companion to JSONL export.
- Always-on `/healthz` JSON endpoint for k8s probes.
- `Dockerfile` (multi-stage, non-root, with `HEALTHCHECK`) and
  `.dockerignore`.
- `Makefile` with `install / test / lint / type / fmt / smoke / run /
  ci / clean`.
- `py.typed` marker — package now ships PEP 561 type information.
- `CONTRIBUTING.md`, `.editorconfig`, GitHub issue + PR templates.

### Changed

- `/healthz` and `/metrics` switched from Mount to Starlette `Route` so
  bare paths (no trailing slash) no longer return a 307 redirect.
- Smoke script now checks `/healthz`, the full v0.2.0 tool inventory,
  and round-trips `audit_get_call_stats` in addition to
  `audit_get_recent_calls`.
- `mypy` config tightened (`warn_unused_ignores`, `check_untyped_defs`,
  `no_implicit_optional`, `warn_redundant_casts`).

## [0.2.0] — 2026-04-26

### Added

- **`audit_get_slowest_calls`** tool — returns the N longest-duration calls
  overall, useful for spotting pathological invocations or downstream
  regressions.
- **`audit_get_top_errors`** tool — groups failed calls by `(tool_name,
  error)` and returns the most frequent, with occurrences and last-seen.
- **`audit_get_top_consumers`** tool — groups calls by `client_info` and
  returns per-client call counts, error counts, error rate, and last-seen.
- **`audit_vacuum`** tool — runs SQLite `VACUUM` to reclaim disk space after
  a large purge. Returns size-before / size-after / reclaimed bytes.
- **p50 / p95 percentiles** now reported alongside avg/min/max in
  `audit_get_call_stats`.
- **Prometheus `/metrics` endpoint**, opt-in via `AUDIT_ENABLE_METRICS`.
  Synthesized from the audit DB on each scrape — no in-process counters,
  no `prometheus_client` dependency. Series: `mcp_audit_calls_total`,
  `mcp_audit_call_duration_seconds` (summary), `mcp_audit_db_size_bytes`.
  Gated by the same bearer token as `/mcp` when one is set.
- **Per-call client info.** Each row now records the client IP, user-agent,
  and a presence marker for bearer auth (never the token itself), captured
  via a `ContextVar` set in the outer ASGI wrapper.
- **`audit_search_arguments`** gained two optional fields:
  `tool_name` (equality filter) and `include_response` (also LIKE-match
  against the response payload).
- **`scripts/smoke.py`** — end-to-end smoke test that boots the proxy,
  opens a real Streamable HTTP MCP session, and round-trips an
  `audit_get_recent_calls` call. Exits non-zero on failure. Useful for
  catching SDK-version drift in CI.
- **`--version`** CLI flag.
- **HTTP downstream header propagation tests** — mock `streamable_http_client`
  and assert the configured headers land on the `httpx.AsyncClient` the
  SDK uses for every request.

### Changed

- Tool count grew from 8 to 12.

## [0.1.0] — 2026-04-19

### Added

- Core proxy for stdio + Streamable HTTP downstream MCP servers.
- SQLite audit store with WAL mode and bounded payload truncation.
- Eight `audit_*` query tools.
- Optional bearer-token auth on the HTTP endpoint.
- Time-based retention (`AUDIT_RETENTION_DAYS`).
- Config loader with CLI > env > file > defaults precedence.
- Structured JSON logging on stderr.
- CI matrix: Python 3.10/3.11/3.12 × ruff + mypy + pytest.
- Release workflow: PyPI trusted-publisher on tag push.
