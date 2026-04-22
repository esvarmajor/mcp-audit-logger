"""SQLite-backed persistence for audit logs.

Design notes
------------
* One table, `audit_calls`, one row per tool call.
* WAL journal mode so reads (audit_get_*) never block the writer.
* A process-wide `threading.Lock` serializes writes — SQLite in WAL mode supports
  concurrent readers but only one writer at a time, and we want clean error handling
  rather than SQLITE_BUSY retries bubbling into tool-call latency.
* JSON payloads are truncated to `max_payload_bytes` to keep the DB bounded even
  when an upstream tool emits large responses.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_calls (
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
CREATE INDEX IF NOT EXISTS idx_audit_ts_start  ON audit_calls(ts_start);
CREATE INDEX IF NOT EXISTS idx_audit_tool_name ON audit_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_audit_success   ON audit_calls(success);
"""


class AuditStore:
    """Thread-safe SQLite wrapper for audit log persistence."""

    def __init__(self, db_path: Path | str, max_payload_bytes: int = 64 * 1024) -> None:
        self._db_path = Path(db_path)
        self._max_payload = max_payload_bytes
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")

    # ------------------------------------------------------------------ internals

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=30.0,
            isolation_level=None,  # autocommit; we use explicit transactions if needed
        )
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _truncate(self, payload: Any) -> str | None:
        """Serialize to JSON; if oversized, preserve a head snippet and mark truncated."""
        if payload is None:
            return None
        try:
            s = json.dumps(payload, default=str)
        except (TypeError, ValueError):
            s = json.dumps(str(payload))
        encoded = s.encode("utf-8")
        if len(encoded) > self._max_payload:
            head = s[: self._max_payload // 2]
            return json.dumps(
                {
                    "_truncated": True,
                    "_original_bytes": len(encoded),
                    "head": head,
                }
            )
        return s

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for key in ("arguments_json", "response_json"):
            val = d.pop(key)
            out_key = key.removesuffix("_json")
            if val is None:
                d[out_key] = None
            else:
                try:
                    d[out_key] = json.loads(val)
                except (TypeError, ValueError):
                    d[out_key] = val
        d["success"] = bool(d["success"])
        return d

    # ----------------------------------------------------------------------- API

    def log_call(
        self,
        *,
        ts_start: float,
        ts_end: float,
        tool_name: str,
        arguments: Any,
        response: Any,
        success: bool,
        error: str | None,
        client_info: str | None = None,
        downstream_target: str | None = None,
    ) -> int:
        """Persist one audit record. Returns the new row id."""
        duration_ms = (ts_end - ts_start) * 1000.0
        args_json = self._truncate(arguments)
        resp_json = self._truncate(response)
        with self._lock, self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO audit_calls
                    (ts_start, ts_end, duration_ms, tool_name, arguments_json, response_json,
                     success, error, client_info, downstream_target)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_start,
                    ts_end,
                    duration_ms,
                    tool_name,
                    args_json,
                    resp_json,
                    1 if success else 0,
                    error,
                    client_info,
                    downstream_target,
                ),
            )
            return int(cur.lastrowid)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM audit_calls ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def by_tool(self, tool_name: str, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM audit_calls WHERE tool_name = ? ORDER BY id DESC LIMIT ?",
                (tool_name, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def failed(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM audit_calls WHERE success = 0 ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def stats(self) -> list[dict[str, Any]]:
        """Per-tool aggregate stats, ordered by call count desc."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT tool_name,
                       COUNT(*)                                           AS call_count,
                       AVG(duration_ms)                                   AS avg_duration_ms,
                       MIN(duration_ms)                                   AS min_duration_ms,
                       MAX(duration_ms)                                   AS max_duration_ms,
                       SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END)       AS error_count,
                       CAST(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS REAL)
                           / COUNT(*)                                     AS error_rate
                FROM audit_calls
                GROUP BY tool_name
                ORDER BY call_count DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]
