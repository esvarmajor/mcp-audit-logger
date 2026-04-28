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
- **Eight query/management tools for agents.** An agent connected through the
  logger can introspect and manage its own call history:
    - `audit_get_recent_calls` — newest N calls
    - `audit_get_calls_by_tool` — filter by tool name
    - `audit_get_failed_calls` — only errored calls
    - `audit_get_call_stats` — per-tool aggregate metrics
    - `audit_get_calls_in_range` — time-bounded slice
    - `audit_search_arguments` — SQL LIKE search over argument JSON
    - `audit_export_jsonl` — export as JSONL for offline analysis
    - `audit_purge` — delete old rows (with dry-run protection)
- **Seven observability tools** for metrics, traces, and incident triage —
  see [Observability extensions](#observability-extensions). The composite
  `obs_investigate` tool is the headline: one MCP call, four backends fanned
  out concurrently, structured result with a deterministic English summary.
- **Streamable HTTP transport.** No SSE — deprecated as of the 2025-03-26 MCP
  spec revision.
- **Structured JSON logs on stderr.** One JSON object per line, ready for any
  ingestion pipeline (Vector, Loki, Datadog, etc.).
- **Bounded payload storage.** Large request/response payloads are truncated
  with a head snippet preserved, so the DB stays predictable.

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

### Observability backends

These power the `obs_*` tools. All optional with localhost defaults — to
explicitly disable a backend, set its env var to an empty string.

| Key                  | Env var (preferred)         | Env var (fallback)  | Default                  |
| -------------------- | --------------------------- | ------------------- | ------------------------ |
| `prometheus_url`     | `AUDIT_PROMETHEUS_URL`      | `PROMETHEUS_URL`    | `http://localhost:9090`  |
| `tempo_url`          | `AUDIT_TEMPO_URL`           | `TEMPO_URL`         | `http://localhost:3200`  |
| `jaeger_url`         | `AUDIT_JAEGER_URL`          | `JAEGER_URL`        | `http://localhost:16686` |
| `alertmanager_url`   | `AUDIT_ALERTMANAGER_URL`    | `ALERTMANAGER_URL`  | `http://localhost:9093`  |
| `metric_names`       | `AUDIT_METRIC_NAMES` (JSON) | —                   | OTLP semconv (see below) |

If both `tempo_url` and `jaeger_url` are configured, Tempo wins. If neither
is set, trace tools return `{ "error": "not_configured", "backend": "trace", ... }`.

Default metric names follow OpenTelemetry HTTP semantic conventions. Override
any subset via `metric_names` in the config file or `AUDIT_METRIC_NAMES` as
JSON:

```json
{
  "metric_names": {
    "request_count": "http_requests_total",
    "request_duration": "http_request_duration_seconds",
    "service_label": "service",
    "status_label": "status"
  }
}
```

## The audit_* tools

| Tool                        | Input                                                    | Output                                   |
| --------------------------- | -------------------------------------------------------- | ---------------------------------------- |
| `audit_get_recent_calls`    | `{ "limit": 50 }`                                        | Newest-first array of call records.      |
| `audit_get_calls_by_tool`   | `{ "tool_name": "...", "limit": 50 }`                    | Newest-first array filtered by name.     |
| `audit_get_failed_calls`    | `{ "limit": 50 }`                                        | Newest-first array of `success = false`. |
| `audit_get_call_stats`      | `{}`                                                     | Per-tool aggregates (see below).         |
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
  "client_info": null,
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
    "error_count": 3,
    "error_rate": 0.0211
  }
]
```

## Observability extensions

Alongside the `audit_*` tools, the proxy exposes seven `obs_*` tools that
turn it into a single point of contact for incident investigation. Metrics
go to Prometheus, traces go to Tempo or Jaeger, alerts go to AlertManager,
and a composite `obs_investigate` tool fans out across all four (plus the
local audit log) concurrently.

| Tool                              | Input                                                          | Returns                                       |
| --------------------------------- | -------------------------------------------------------------- | --------------------------------------------- |
| `obs_query_metric`                | `{ expr, start, end, step }`                                   | Raw PromQL range query result.                |
| `obs_get_service_metrics`         | `{ service, window }`                                          | RED metrics: error rate, RPS, p50/p95/p99.    |
| `obs_list_instrumented_services`  | `{ base_metric? }`                                             | Discovery via Prometheus label values.        |
| `obs_get_trace`                   | `{ trace_id }`                                                 | Recursive span tree (parent-child).           |
| `obs_find_traces`                 | `{ service, start, end, min_duration_ms?, error_only?, limit?}`| Trace metadata for triage.                    |
| `obs_get_slow_spans`              | `{ service, window, threshold_ms }`                            | Spans grouped by operation with p99 + samples.|
| `obs_investigate`                 | `{ service, start, end }`                                      | Metrics delta + traces + logs + alerts + summary. |

Times are ISO8601 (`2024-01-01T14:00:00Z`). Windows are Prometheus duration
syntax (`5m`, `1h`, `30s`).

### Error envelope

All `obs_*` tools return a top-level JSON object. Errors look like:

```json
{ "error": "not_configured", "backend": "tempo", "message": "Set TEMPO_URL to enable trace queries" }
{ "error": "backend_unreachable", "backend": "prometheus", "message": "ConnectError: ..." }
{ "error": "upstream_error", "backend": "jaeger", "status": 500, "message": "..." }
{ "error": "trace_not_found", "trace_id": "...", "backend": "tempo" }
```

When a tool returns one of these, `CallToolResult.isError` is `true`. Network
errors never propagate as exceptions across the MCP boundary.

### `obs_investigate` shape

```json
{
  "service": "checkout",
  "window": { "start": "2024-04-27T14:30:00Z", "end": "2024-04-27T14:45:00Z" },
  "prior_window": { "start": "2024-04-27T14:15:00Z", "end": "2024-04-27T14:30:00Z" },
  "metrics_delta": {
    "error_rate":     { "current": 0.12, "prior": 0.03, "delta_pct": 300.0 },
    "request_rate_rps": { "current": 45,  "prior": 45,  "delta_pct": 0 },
    "latency_p99_ms":  { "current": 2400, "prior": 800, "delta_pct": 200.0 }
  },
  "anomalous_traces": {
    "errored": [ { "trace_id": "...", "root_name": "POST /api/checkout", "duration_ms": 1200, "error": true } ],
    "slowest": [ /* top 3 by duration */ ]
  },
  "log_sample": [ /* up to 20 recent audit rows */ ],
  "alerts":     { "active": [...], "recently_resolved": [...] },
  "summary":    "Error rate spiked 4.0x vs prior window. 87% of errored traces share operation: POST /api/checkout. Active alerts: HighErrorRate.",
  "errors":     []
}
```

If a backend is unreachable, the corresponding key is `null` and `errors[]`
gets `{ step, backend, message }`. The summary is still generated from
whatever data did arrive.

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
pip install -e '.[dev]'
pytest            # storage + server integration tests
ruff check .      # lint
mypy src/mcp_audit_logger --ignore-missing-imports
```

## License

MIT. See [LICENSE](LICENSE).
