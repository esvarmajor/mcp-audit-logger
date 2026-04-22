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


@overload
def _env(name: str, default: str) -> str: ...
@overload
def _env(name: str, default: None = ...) -> str | None: ...
def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


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
    cfg.host = str(cfg_data.get("host", _env("AUDIT_HOST", cfg.host)))
    cfg.port = int(cfg_data.get("port", _env("AUDIT_PORT", str(cfg.port))))
    cfg.mount_path = str(cfg_data.get("mount_path", _env("AUDIT_MOUNT_PATH", cfg.mount_path)))
    cfg.db_path = Path(str(cfg_data.get("db_path", _env("AUDIT_DB_PATH", str(cfg.db_path)))))
    cfg.log_level = str(cfg_data.get("log_level", _env("AUDIT_LOG_LEVEL", cfg.log_level)))
    cfg.max_payload_bytes = int(
        cfg_data.get(
            "max_payload_bytes",
            _env("AUDIT_MAX_PAYLOAD_BYTES", str(cfg.max_payload_bytes)),
        )
    )
    cfg.http_token = cfg_data.get("http_token") or _env("AUDIT_HTTP_TOKEN") or None

    downstream_data = cfg_data.get("downstream") or _load_downstream_from_env()
    if downstream_data:
        cfg.downstream = _parse_downstream(downstream_data)

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
