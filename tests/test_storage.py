"""Unit tests for the SQLite audit store.

These tests only exercise `AuditStore` and have no dependency on the MCP SDK,
so they're the fastest way to verify persistence behavior.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mcp_audit_logger.storage import AuditStore


@pytest.fixture()
def store(tmp_path: Path) -> AuditStore:
    return AuditStore(tmp_path / "audit.db")


def _insert(
    store: AuditStore,
    *,
    tool: str = "fetch",
    success: bool = True,
    error: str | None = None,
    args: object = None,
    response: object = None,
    duration_s: float = 0.1,
) -> int:
    t0 = time.time()
    return store.log_call(
        ts_start=t0,
        ts_end=t0 + duration_s,
        tool_name=tool,
        arguments=args if args is not None else {"url": "https://example.com"},
        response=response if response is not None else {"ok": True},
        success=success,
        error=error,
    )


def test_log_and_recent_round_trip(store: AuditStore) -> None:
    rid = _insert(store)
    rows = store.recent(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == rid
    assert row["tool_name"] == "fetch"
    assert row["success"] is True
    assert row["arguments"] == {"url": "https://example.com"}
    assert row["response"] == {"ok": True}
    assert row["duration_ms"] == pytest.approx(100.0, rel=0.2)


def test_recent_is_newest_first(store: AuditStore) -> None:
    _insert(store, tool="a")
    _insert(store, tool="b")
    _insert(store, tool="c")
    rows = store.recent(limit=10)
    assert [r["tool_name"] for r in rows] == ["c", "b", "a"]


def test_by_tool_filters(store: AuditStore) -> None:
    _insert(store, tool="fetch")
    _insert(store, tool="search")
    _insert(store, tool="fetch")
    rows = store.by_tool("fetch")
    assert len(rows) == 2
    assert all(r["tool_name"] == "fetch" for r in rows)


def test_failed_returns_only_errors(store: AuditStore) -> None:
    _insert(store, success=True)
    _insert(store, success=False, error="boom")
    _insert(store, success=False, error="kapow")
    rows = store.failed()
    assert len(rows) == 2
    assert all(r["success"] is False for r in rows)
    assert {r["error"] for r in rows} == {"boom", "kapow"}


def test_stats_aggregates_per_tool(store: AuditStore) -> None:
    _insert(store, tool="fetch", success=True, duration_s=0.1)
    _insert(store, tool="fetch", success=True, duration_s=0.2)
    _insert(store, tool="fetch", success=False, error="x", duration_s=0.3)
    _insert(store, tool="search", success=True, duration_s=0.05)
    stats = {row["tool_name"]: row for row in store.stats()}
    assert stats["fetch"]["call_count"] == 3
    assert stats["fetch"]["error_count"] == 1
    assert stats["fetch"]["error_rate"] == pytest.approx(1 / 3, rel=1e-3)
    assert stats["fetch"]["min_duration_ms"] == pytest.approx(100.0, rel=0.2)
    assert stats["fetch"]["max_duration_ms"] == pytest.approx(300.0, rel=0.2)
    assert stats["search"]["call_count"] == 1
    assert stats["search"]["error_count"] == 0


def test_large_payload_is_truncated(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.db", max_payload_bytes=256)
    big = "x" * 10_000
    _insert(store, response={"blob": big})
    row = store.recent(limit=1)[0]
    assert row["response"]["_truncated"] is True
    assert row["response"]["_original_bytes"] > 256
    assert "head" in row["response"]


def test_non_json_serializable_args_coerce_via_default(store: AuditStore) -> None:
    # Sets aren't JSON-serializable; json.dumps(..., default=str) coerces them
    # rather than crashing. The surrounding dict shape is preserved.
    _insert(store, args={"set": {1, 2, 3}})
    row = store.recent(limit=1)[0]
    assert isinstance(row["arguments"], dict)
    assert isinstance(row["arguments"]["set"], str)
    assert row["arguments"]["set"].startswith("{") and row["arguments"]["set"].endswith("}")


def test_limit_clamps_are_applied(store: AuditStore) -> None:
    for i in range(5):
        _insert(store, tool=f"t{i}")
    # Negative / zero / huge limits should still yield sane results.
    assert len(store.recent(limit=0)) == 1  # clamped up to min 1
    assert len(store.recent(limit=10_000)) == 5  # capped by available rows
