"""Configuration loading for mcp-audit-logger.

Configuration is resolved in this order (later sources override earlier ones):

    1. Defaults defined here.
    2. JSON config file (if one is passed via --config or the AUDIT_CONFIG env var).
    3. Environment variables (AUDIT_*).
    4. CLI flags (handled in __main__.py).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, overload


@dataclass
class DownstreamStdio:
    """Connection info for a downstream MCP server spoken via stdio."""

    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    kind: str = "stdio"


@dataclass
class DownstreamHttp:
    """Connection info for a downstream MCP server spoken via Streamable HTTP."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    kind: str = "http"


Downstream = DownstreamStdio | DownstreamHttp


@dataclass
class Config:
    """Runtime configuration for the audit logger proxy."""

    # Where we expose our own Streamable HTTP endpoint.
    host: str = "127.0.0.1"
    port: int = 8765

    # Mount path for the MCP endpoint.
    mount_path: str = "/mcp"

    # SQLite database file.
    db_path: Path = field(default_factory=lambda: Path("./audit.db"))

    # Downstream server we proxy to. If None, we still run and expose
    # only the audit_* query tools — useful for read-only consumption
    # of an existing audit.db.
    downstream: Downstream | None = None

    # JSON log level.
    log_level: str = "INFO"

    # Max argument/response payload size to persist per call (bytes).
    # Larger payloads are truncated with a head snippet preserved.
    max_payload_bytes: int = 64 * 1024

    # Optional bearer token. If set, every HTTP request to the proxy must carry
    # `Authorization: Bearer <token>` or receive a 401 response.
    http_token: str | None = None

    # Delete audit rows older than this many days once per hour.
    # Set to 0 or None to disable retention (default: no retention).
    retention_days: int | None = None

    # Observability backends. URLs default to standard local ports; set the
    # env var to an empty string to explicitly disable a backend (the client
    # will not be instantiated and tools requiring it return a structured
    # `not_configured` error).
    prometheus_url: str | None = "http://localhost:9090"
    tempo_url: str | None = "http://localhost:3200"
    jaeger_url: str | None = "http://localhost:16686"
    alertmanager_url: str | None = "http://localhost:9093"

    # Optional override of metric names used by obs_get_service_metrics.
    # Empty dict means "use OTLP semantic-convention defaults".
    metric_names: dict[str, str] = field(default_factory=dict)


@overload
def _env(name: str, default: str) -> str: ...
@overload
def _env(name: str, default: None = ...) -> str | None: ...
def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def _env_present(*names: str) -> tuple[bool, str | None]:
    """Return (was_set, value) for the first env var present in the environment.

    Differs from `_env` in that an explicitly-set empty string returns
    (True, "") so callers can treat it as "explicit disable" rather than
    falling back to a default. Returns (False, None) if no name is set.
    """
    for n in names:
        if n in os.environ:
            return True, os.environ[n]
    return False, None


def load_config(config_path: str | Path | None = None) -> Config:
    """Build a Config from the JSON file (optional) and environment overrides."""
    cfg_data: dict[str, Any] = {}
    path = config_path or _env("AUDIT_CONFIG")
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        cfg_data = json.loads(p.read_text())

    cfg = Config()
    # Layer: defaults → JSON file → env (later layers win).
    if "host" in cfg_data:
        cfg.host = str(cfg_data["host"])
    if (v := _env("AUDIT_HOST")):
        cfg.host = v

    if "port" in cfg_data:
        cfg.port = int(cfg_data["port"])
    if (v := _env("AUDIT_PORT")):
        cfg.port = int(v)

    if "mount_path" in cfg_data:
        cfg.mount_path = str(cfg_data["mount_path"])
    if (v := _env("AUDIT_MOUNT_PATH")):
        cfg.mount_path = v

    if "db_path" in cfg_data:
        cfg.db_path = Path(str(cfg_data["db_path"]))
    if (v := _env("AUDIT_DB_PATH")):
        cfg.db_path = Path(v)

    if "log_level" in cfg_data:
        cfg.log_level = str(cfg_data["log_level"])
    if (v := _env("AUDIT_LOG_LEVEL")):
        cfg.log_level = v

    if "max_payload_bytes" in cfg_data:
        cfg.max_payload_bytes = int(cfg_data["max_payload_bytes"])
    if (v := _env("AUDIT_MAX_PAYLOAD_BYTES")):
        cfg.max_payload_bytes = int(v)

    cfg.http_token = cfg_data.get("http_token") or _env("AUDIT_HTTP_TOKEN") or None

    if "retention_days" in cfg_data:
        cfg.retention_days = int(cfg_data["retention_days"]) or None
    if (v := _env("AUDIT_RETENTION_DAYS")):
        cfg.retention_days = int(v) or None

    downstream_data = cfg_data.get("downstream") or _load_downstream_from_env()
    if downstream_data:
        cfg.downstream = _parse_downstream(downstream_data)

    # Observability URLs. AUDIT_-prefixed name takes precedence over the
    # bare name (per spec). Setting either to empty string disables.
    for attr, audit_name, bare_name in (
        ("prometheus_url", "AUDIT_PROMETHEUS_URL", "PROMETHEUS_URL"),
        ("tempo_url", "AUDIT_TEMPO_URL", "TEMPO_URL"),
        ("jaeger_url", "AUDIT_JAEGER_URL", "JAEGER_URL"),
        ("alertmanager_url", "AUDIT_ALERTMANAGER_URL", "ALERTMANAGER_URL"),
    ):
        if attr in cfg_data:
            setattr(cfg, attr, str(cfg_data[attr]))
        was_set, value = _env_present(audit_name, bare_name)
        if was_set:
            setattr(cfg, attr, value)

    if "metric_names" in cfg_data and isinstance(cfg_data["metric_names"], dict):
        cfg.metric_names = {str(k): str(v) for k, v in cfg_data["metric_names"].items()}
    if (v := _env("AUDIT_METRIC_NAMES")):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, dict):
                cfg.metric_names = {str(k): str(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, TypeError):
            pass

    return cfg


def _load_downstream_from_env() -> dict[str, Any] | None:
    """Materialize downstream config from AUDIT_DOWNSTREAM_* env vars, if set."""
    if url := _env("AUDIT_DOWNSTREAM_URL"):
        headers: dict[str, str] = {}
        if hdrs := _env("AUDIT_DOWNSTREAM_HEADERS"):
            headers = json.loads(hdrs)
        return {"kind": "http", "url": url, "headers": headers}

    if cmd := _env("AUDIT_DOWNSTREAM_COMMAND"):
        args_env = _env("AUDIT_DOWNSTREAM_ARGS", "") or ""
        # Accept either a JSON array ("[\"--flag\", \"x\"]") or a plain shell-split string.
        args = json.loads(args_env) if args_env.startswith("[") else args_env.split()
        env_env = _env("AUDIT_DOWNSTREAM_ENV")
        downstream_env = json.loads(env_env) if env_env else {}
        return {"kind": "stdio", "command": cmd, "args": args, "env": downstream_env}

    return None


def _parse_downstream(d: dict[str, Any]) -> Downstream:
    kind = d.get("kind", "stdio")
    if kind == "http":
        return DownstreamHttp(url=d["url"], headers=d.get("headers", {}) or {})
    return DownstreamStdio(
        command=d["command"],
        args=list(d.get("args", []) or []),
        env=dict(d.get("env", {}) or {}),
        cwd=d.get("cwd"),
    )
