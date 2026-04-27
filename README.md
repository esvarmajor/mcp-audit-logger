# mcp-audit-logger

A transparent middleware proxy for the [Model Context Protocol](https://modelcontextprotocol.io/).
It sits between any MCP client and any MCP server, forwards every tool call untouched,
and records a full audit trail to a local SQLite database — arguments, response,
timestamp, duration, and success/failure. It also exposes its own MCP tools so
your agent can query the audit history at runtime.

```
┌──────────────┐   Streamable HTTP   ┌────────────────────┐   stdio or HTTP   ┌────────────────┐
│  MCP Client  │ ──────────────────▶ │  mcp-audit-logger  │ ────────────────▶ │  MCP Server X  │
│  (agent/IDE) │ ◀────────────────── │     (this proxy)   │ ◀──────────────── │  (real tools)  │
└──────────────┘                     └─────────┬──────────┘                   └────────────────┘
                                               │
                                               ▼
                                        ┌─────────────┐
                                        │  audit.db   │   ← SQLite, queryable via audit_* tools
                                        └─────────────┘
```

## Why

The MCP ecosystem is moving fast, and agents are starting to call real tools
against real systems. If something goes wrong — a bad write, a runaway loop,
a misrouted call — you want a reliable, append-only record of what happened,
independent of any individual server's logging. This project is that record.

## Features

- **Transparent proxy.** Any tool your downstream server exposes shows up to
  the client with its original name, schema, and behavior. Clients don't need
  to know the logger is there.
- **Full audit trail in SQLite.** One row per tool call. No external DB, no
  Kafka, nothing to operate.
- **Seventeen query/management tools for agents.** An agent connected through the
  logger can introspect and manage its own call history:
    - `audit_get_recent_calls` — newest N calls
    - `audit_get_call_by_id` — fetch a single row for deep-dive debugging
    - `audit_get_calls_by_tool` — filter by tool name
    - `audit_get_calls_by_client` — filter by client_info LIKE pattern
    - `audit_get_failed_calls` — only errored calls
    - `audit_get_recent_failures` — failures in the last N seconds
    - `audit_count` — fast COUNT(*) with optional filters
    - `audit_get_call_stats` — per-tool aggregate metrics (count, avg/min/max/p50/p95, error rate)
    - `audit_get_slowest_calls` — N slowest calls overall, longest first
    - `audit_get_top_errors` — most frequent (tool, error) groupings
    - `audit_get_top_consumers` — most active clients (by IP/UA)
    - `audit_get_calls_in_range` — time-bounded slice
    - `audit_search_arguments` — SQL LIKE search over argument JSON
    - `audit_export_jsonl` — export as JSONL for offline analysis
    - `audit_export_csv` — export as CSV for spreadsheet workflows
    - `audit_vacuum` — run SQLite VACUUM to reclaim space after a purge
    - `audit_purge` — delete old rows (with dry-run protection)
- **Streamable HTTP transport.** No SSE — deprecated as of the 2025-03-26 MCP
  spec revision.
- **Structured JSON logs on stderr.** One JSON object per line, ready for any
  ingestion pipeline (Vector, Loki, Datadog, etc.).
- **Bounded payload storage.** Large request/response payloads are truncated
  with a head snippet preserved, so the DB stays predictable.
- **Per-call client info.** Each row records the client IP, user-agent, and
  whether bearer auth was in use — handy for incident forensics across
  multiple consumers.

## Requirements

- Python **3.10 or newer** (the MCP SDK does not support 3.9).
- SQLite is provided by the Python stdlib. No external dependencies.

## Install

From source (until this is published on PyPI):

```bash
git clone https://github.com/esvarmajor/mcp-audit-logger.git
cd mcp-audit-logger
python3.10 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Quick start

### 1. Point the proxy at an existing MCP server

The simplest way is environment variables. Suppose you're already running an
MCP server via stdio — e.g. the canonical `mcp-server-fetch`:

```bash
export AUDIT_DOWNSTREAM_COMMAND="python"
export AUDIT_DOWNSTREAM_ARGS='["-m", "mcp_server_fetch"]'
export AUDIT_DB_PATH="./audit.db"

mcp-audit-logger --host 127.0.0.1 --port 8765
```

The proxy is now listening on `http://127.0.0.1:8765/mcp`. Any MCP client that
connects to it will see every tool the fetch server exposes, plus the four
`audit_*` query tools — and every call will be logged to `./audit.db`.

### 2. Or use a JSON config file

```bash
mcp-audit-logger --config ./examples/config.json
```

Example config (`examples/config.json`):

```json
{
  "host": "127.0.0.1",
  "port": 8765,
  "db_path": "./audit.db",
  "log_level": "INFO",
  "downstream": {
    "kind": "stdio",
    "command": "python",
    "args": ["-m", "mcp_server_fetch"]
  }
}
```

Or to proxy a server that speaks Streamable HTTP:

```json
{
  "host": "127.0.0.1",
  "port": 8765,
  "db_path": "./audit.db",
  "downstream": {
    "kind": "http",
    "url": "http://localhost:9000/mcp",
    "headers": { "Authorization": "Bearer ..." }
  }
}
```

### 3. Wire it into a client

Any MCP client that supports Streamable HTTP can connect. For Claude Desktop,
replace the direct server entry with one that points at the logger:

```jsonc
// ~/Library/Application Support/Claude/claude_desktop_config.json
{
  "mcpServers": {
    "fetch-audited": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

See `examples/claude_desktop_config.json` for a full example.

## Configuration reference

Every option can be set via JSON config file, environment variable, or CLI flag.
CLI beats env beats file.

| Key                  | Env var                     | CLI flag         | Default       |
| -------------------- | --------------------------- | ---------------- | ------------- |
| `host`               | `AUDIT_HOST`                | `--host`         | `127.0.0.1`   |
| `port`               | `AUDIT_PORT`                | `--port`         | `8765`        |
| `mount_path`         | `AUDIT_MOUNT_PATH`          | `--mount-path`   | `/mcp`        |
| `db_path`            | `AUDIT_DB_PATH`             | `--db-path`      | `./audit.db`  |
| `log_level`          | `AUDIT_LOG_LEVEL`           | `--log-level`    | `INFO`        |
| `max_payload_bytes`  | `AUDIT_MAX_PAYLOAD_BYTES`   | —                | `65536`       |
| `http_token`         | `AUDIT_HTTP_TOKEN`          | —                | _(none)_      |
| `retention_days`     | `AUDIT_RETENTION_DAYS`      | —                | _(none)_      |
| `enable_metrics`     | `AUDIT_ENABLE_METRICS`      | —                | `false`       |
| `metrics_path`       | `AUDIT_METRICS_PATH`        | —                | `/metrics`    |
| `health_path`        | `AUDIT_HEALTH_PATH`         | —                | `/healthz`    |
| `downstream.kind`    | (see below)                 | —                | —             |

Downstream via env vars:

| Env var                        | Meaning                                                        |
| ------------------------------ | -------------------------------------------------------------- |
| `AUDIT_DOWNSTREAM_URL`         | If set, use Streamable HTTP transport at this URL.             |
| `AUDIT_DOWNSTREAM_HEADERS`     | JSON dict of headers for HTTP downstream.                      |
| `AUDIT_DOWNSTREAM_COMMAND`     | Otherwise, use stdio transport spawning this command.          |
| `AUDIT_DOWNSTREAM_ARGS`        | Shell-split string or JSON array of args.                      |
| `AUDIT_DOWNSTREAM_ENV`         | JSON dict of env vars to pass to the stdio child.              |

If no downstream is configured, the logger still runs — it just exposes the
`audit_*` query tools against whatever is already in `audit.db`. Useful for
offline analysis.

## The audit_* tools

| Tool                        | Input                                                    | Output                                   |
| --------------------------- | -------------------------------------------------------- | ---------------------------------------- |
| `audit_get_recent_calls`    | `{ "limit": 50 }`                                        | Newest-first array of call records.      |
| `audit_get_call_by_id`      | `{ "call_id": 42 }`                                      | Single row for the given primary key.    |
| `audit_get_calls_by_tool`   | `{ "tool_name": "...", "limit": 50 }`                    | Newest-first array filtered by name.     |
| `audit_get_failed_calls`    | `{ "limit": 50 }`                                        | Newest-first array of `success = false`. |
| `audit_get_call_stats`      | `{}`                                                     | Per-tool aggregates (see below).         |
| `audit_get_slowest_calls`   | `{ "limit": 20 }`                                        | Slowest-first array of call records.     |
| `audit_get_top_errors`      | `{ "limit": 20 }`                                        | `(tool, error)` groups with occurrences. |
| `audit_get_top_consumers`   | `{ "limit": 20 }`                                        | Per-client call/error counts and rate.   |
| `audit_vacuum`              | `{}`                                                     | Size before/after + reclaimed bytes.     |
| `audit_get_calls_in_range`  | `{ "start_ts": 1713600000, "end_ts": 1713700000 }`       | Calls in the given Unix-ts window.       |
| `audit_search_arguments`    | `{ "pattern": "%example.com%", "limit": 50 }`            | Calls whose arguments match the pattern. |
| `audit_export_jsonl`        | `{ "limit": 500, "since_id": 0 }`                        | JSONL text, one record per line.         |
| `audit_purge`               | `{ "before_ts": 1713600000, "dry_run": true }`           | Count of rows deleted (or would-delete). |

A call record looks like:

```json
{
  "id": 42,
  "ts_start": 1713660012.123,
  "ts_end": 1713660012.456,
  "duration_ms": 333.0,
  "tool_name": "fetch",
  "arguments": { "url": "https://example.com" },
  "response": { "content": [{ "type": "text", "text": "..." }], "isError": false },
  "success": true,
  "error": null,
  "client_info": "{\"ip\":\"127.0.0.1\",\"ua\":\"claude-desktop/0.1.234\",\"auth\":\"bearer\"}",
  "downstream_target": "stdio:python -m mcp_server_fetch"
}
```

`audit_get_call_stats` returns:

```json
[
  {
    "tool_name": "fetch",
    "call_count": 142,
    "avg_duration_ms": 284.1,
    "min_duration_ms": 41.0,
    "max_duration_ms": 3120.0,
    "p50_duration_ms": 240.0,
    "p95_duration_ms": 1190.0,
    "error_count": 3,
    "error_rate": 0.0211
  }
]
```

## SQLite schema

```sql
CREATE TABLE audit_calls (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_start           REAL    NOT NULL,
    ts_end             REAL    NOT NULL,
    duration_ms        REAL    NOT NULL,
    tool_name          TEXT    NOT NULL,
    arguments_json     TEXT,
    response_json      TEXT,
    success            INTEGER NOT NULL,
    error              TEXT,
    client_info        TEXT,
    downstream_target  TEXT
);
```

The DB runs in WAL mode, so you can `sqlite3 audit.db` and run arbitrary
queries while the logger is live.

## Health check

The proxy always exposes a small JSON `/healthz` endpoint (path configurable
via `AUDIT_HEALTH_PATH`). It returns 200 when the audit DB is writable and
the downstream (if configured) has a live session — 503 otherwise. Suitable
for k8s liveness/readiness probes. Never gated by bearer auth.

```bash
$ curl http://127.0.0.1:8765/healthz
{"status":"ok","db_writable":true,"downstream":"connected","version":"0.2.0"}
```

## Prometheus metrics

The proxy can expose a Prometheus-compatible `/metrics` endpoint synthesized
from the audit DB on each scrape. No `prometheus_client` dep required — the
DB is the source of truth, so the metrics survive restarts and never drift.

```bash
AUDIT_ENABLE_METRICS=true mcp-audit-logger
# scrape:
curl http://127.0.0.1:8765/metrics
```

Series exposed:

| Series                                                  | Type    | Labels                  |
| ------------------------------------------------------- | ------- | ----------------------- |
| `mcp_audit_calls_total`                                 | counter | `tool`, `success`       |
| `mcp_audit_call_duration_seconds`                       | summary | `tool`, `quantile`      |
| `mcp_audit_call_duration_seconds_sum`                   |         | `tool`                  |
| `mcp_audit_call_duration_seconds_count`                 |         | `tool`                  |
| `mcp_audit_db_size_bytes`                               | gauge   | _(none)_                |

If `AUDIT_HTTP_TOKEN` is set, the metrics endpoint is gated by the same bearer
token as `/mcp`. If you need un-authed scraping, terminate auth in a reverse
proxy in front of the logger.

## Bearer auth

If `AUDIT_HTTP_TOKEN` (or `http_token` in the config file) is set, every HTTP
request to the proxy must include `Authorization: Bearer <token>` or receive
a `401 Unauthorized` response. Useful when the proxy is exposed on a non-loopback
address.

```bash
AUDIT_HTTP_TOKEN=my-secret-token mcp-audit-logger --host 0.0.0.0 --port 8765
```

```json
{ "http_token": "my-secret-token" }
```

## Development

```bash
make install      # create venv and install in editable mode
make ci           # lint + type-check + run tests
make smoke        # end-to-end smoke test
```

Or manually, without the Makefile:

```bash
pip install -e '.[dev]'
pytest            # storage + server integration tests
ruff check .      # lint
mypy src/mcp_audit_logger --ignore-missing-imports
```

### Smoke test

`scripts/smoke.py` boots the proxy, opens a real Streamable HTTP MCP
session against it, lists tools, and round-trips a call to
`audit_get_recent_calls`. It exits non-zero on any failure — useful for
catching SDK-version drift that unit tests don't see.

```bash
python scripts/smoke.py        # exit 0 on success
SMOKE_PORT=8800 ./scripts/smoke.sh  # alternate port via wrapper
```

## Docker

```bash
docker build -t mcp-audit-logger .
docker run --rm -p 8765:8765 \
  -v "$PWD/audit:/data" \
  -e AUDIT_DB_PATH=/data/audit.db \
  -e AUDIT_DOWNSTREAM_COMMAND=python \
  -e AUDIT_DOWNSTREAM_ARGS='["-m","mcp_server_fetch"]' \
  mcp-audit-logger
```

The image runs as a non-root user, exposes port 8765, persists the audit
DB to a `/data` volume, and registers a `HEALTHCHECK` that pings
`/healthz` every 30 seconds.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
