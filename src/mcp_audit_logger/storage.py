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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def _percentile_sorted(sorted_values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile of a pre-sorted, non-empty sequence.

    Returns None for an empty input. `pct` is a number in [0, 100].
    """
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


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
            return int(cur.lastrowid or 0)

    def get_by_id(self, call_id: int) -> dict[str, Any] | None:
        """Return a single audit row by primary key, or None if missing."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM audit_calls WHERE id = ?",
                (call_id,),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

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
        """Per-tool aggregate stats, ordered by call count desc.

        Returns count, error count/rate, and a duration distribution
        (avg, min, max, p50, p95). Percentiles are computed in Python
        because SQLite has no native percentile_cont().
        """
        with self._conn() as c:
            agg_rows = c.execute(
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
            durations: dict[str, list[float]] = {}
            if agg_rows:
                for tool, dur in c.execute(
                    "SELECT tool_name, duration_ms FROM audit_calls"
                ).fetchall():
                    durations.setdefault(tool, []).append(dur)

        out: list[dict[str, Any]] = []
        for r in agg_rows:
            d = dict(r)
            ds = sorted(durations.get(d["tool_name"], []))
            d["p50_duration_ms"] = _percentile_sorted(ds, 50)
            d["p95_duration_ms"] = _percentile_sorted(ds, 95)
            out.append(d)
        return out

    def slowest(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return the slowest N calls overall, longest duration first."""
        limit = max(1, min(limit, 1000))
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM audit_calls ORDER BY duration_ms DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def top_errors(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Group failed calls by (tool_name, error) and return the most frequent.

        Each entry includes occurrences, last_seen (Unix ts), and a sample
        argument JSON from the most recent occurrence.
        """
        limit = max(1, min(limit, 1000))
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT tool_name,
                       error,
                       COUNT(*)                AS occurrences,
                       MAX(ts_start)           AS last_seen
                FROM audit_calls
                WHERE success = 0 AND error IS NOT NULL
                GROUP BY tool_name, error
                ORDER BY occurrences DESC, last_seen DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def in_range(self, *, start_ts: float, end_ts: float, limit: int = 200) -> list[dict[str, Any]]:
        """Return calls whose ts_start falls in [start_ts, end_ts]."""
        limit = max(1, min(limit, 5000))
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM audit_calls WHERE ts_start >= ? AND ts_start <= ? "
                "ORDER BY id DESC LIMIT ?",
                (start_ts, end_ts, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def search_arguments(
        self,
        *,
        pattern: str,
        tool_name: str | None = None,
        include_response: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """SQL LIKE search over the serialised JSON columns.

        ``include_response=True`` widens the search to also match against the
        response payload (useful when you remember a substring of what came
        back rather than what went in). ``tool_name`` is an optional equality
        filter on the call's tool name.
        """
        limit = max(1, min(limit, 1000))
        match_clauses = ["arguments_json LIKE ?"]
        params: list[Any] = [pattern]
        if include_response:
            match_clauses = ["(arguments_json LIKE ? OR response_json LIKE ?)"]
            params = [pattern, pattern]
        if tool_name:
            match_clauses.append("tool_name = ?")
            params.append(tool_name)
        sql = (
            "SELECT * FROM audit_calls "
            f"WHERE {' AND '.join(match_clauses)} "
            "ORDER BY id DESC LIMIT ?"
        )
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, tuple(params)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def export(self, *, limit: int = 500, since_id: int = 0) -> list[dict[str, Any]]:
        """Return rows with id > since_id, oldest first, up to limit. For JSONL export."""
        limit = max(1, min(limit, 10000))
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM audit_calls WHERE id > ? ORDER BY id ASC LIMIT ?",
                (since_id, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def top_consumers(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Group calls by client_info and return the top N callers.

        Useful when multiple agents/clients share one proxy — surfaces who
        is generating the most traffic, with a per-client error rate.
        Rows with NULL client_info (e.g. older entries) are bucketed under
        a synthetic ``"(no client info)"`` key.
        """
        limit = max(1, min(limit, 1000))
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT
                    COALESCE(client_info, '(no client info)') AS client_info,
                    COUNT(*)                                   AS call_count,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS error_count,
                    CAST(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS REAL)
                        / COUNT(*)                             AS error_rate,
                    MAX(ts_start)                              AS last_seen
                FROM audit_calls
                GROUP BY client_info
                ORDER BY call_count DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def vacuum(self) -> None:
        """Run SQLite VACUUM to reclaim disk space after a large purge.

        Cannot run inside a transaction. We open a fresh connection without
        the WAL-shared write lock so concurrent reads still work; VACUUM
        itself blocks writes for its duration, which is unavoidable.
        """
        with self._lock:
            conn = sqlite3.connect(str(self._db_path), timeout=60.0, isolation_level=None)
            try:
                conn.execute("VACUUM")
            finally:
                conn.close()

    def db_size_bytes(self) -> int:
        """Return the on-disk size of the SQLite file in bytes (0 if missing)."""
        try:
            return self._db_path.stat().st_size
        except FileNotFoundError:
            return 0

    def purge(self, *, before_ts: float, dry_run: bool = True) -> int:
        """Delete rows with ts_start < before_ts. Returns the number of rows affected."""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "SELECT COUNT(*) FROM audit_calls WHERE ts_start < ?", (before_ts,)
            )
            count = int(cur.fetchone()[0])
            if not dry_run and count > 0:
                c.execute("DELETE FROM audit_calls WHERE ts_start < ?", (before_ts,))
        return count
