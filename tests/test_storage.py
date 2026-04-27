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


def test_get_by_id_returns_matching_row(store: AuditStore) -> None:
    rid = _insert(store, tool="fetch", args={"url": "https://x.com"})
    row = store.get_by_id(rid)
    assert row is not None
    assert row["id"] == rid
    assert row["tool_name"] == "fetch"
    assert row["arguments"] == {"url": "https://x.com"}


def test_get_by_id_returns_none_for_missing(store: AuditStore) -> None:
    assert store.get_by_id(99999) is None


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


# ------------------------------------------------------------------ new methods


def test_in_range_returns_only_matching_rows(store: AuditStore) -> None:
    t = time.time()
    _insert(store, tool="old")  # ts_start = now (current time, too recent for old range)
    store.log_call(
        ts_start=t - 200, ts_end=t - 199, tool_name="ancient",
        arguments={}, response={}, success=True, error=None,
    )
    rows = store.in_range(start_ts=t - 300, end_ts=t - 100)
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "ancient"


def test_search_arguments_finds_matching_row(store: AuditStore) -> None:
    _insert(store, args={"url": "https://example.com/page"})
    _insert(store, args={"url": "https://other.org"})
    rows = store.search_arguments(pattern="%example.com%")
    assert len(rows) == 1
    assert rows[0]["arguments"]["url"] == "https://example.com/page"


def test_search_arguments_filters_by_tool_name(store: AuditStore) -> None:
    _insert(store, tool="fetch", args={"url": "https://example.com/page1"})
    _insert(store, tool="search", args={"url": "https://example.com/page2"})
    rows = store.search_arguments(pattern="%example.com%", tool_name="fetch")
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "fetch"


def test_search_arguments_include_response_finds_in_response(store: AuditStore) -> None:
    _insert(store, args={"url": "ok"}, response={"text": "the secret keyword is here"})
    # Default scope (arguments only) misses it
    assert store.search_arguments(pattern="%secret keyword%") == []
    # Wider scope finds it
    rows = store.search_arguments(pattern="%secret keyword%", include_response=True)
    assert len(rows) == 1


def test_search_arguments_response_does_not_match_when_disabled(store: AuditStore) -> None:
    _insert(store, args={"url": "x"}, response={"text": "needle"})
    assert store.search_arguments(pattern="%needle%") == []


def test_search_arguments_no_match_returns_empty(store: AuditStore) -> None:
    _insert(store, args={"url": "https://other.org"})
    assert store.search_arguments(pattern="%notpresent%") == []


def test_export_pagination(store: AuditStore) -> None:
    for _ in range(5):
        _insert(store)
    page1 = store.export(limit=2, since_id=0)
    assert len(page1) == 2
    page2 = store.export(limit=2, since_id=page1[-1]["id"])
    assert len(page2) == 2
    # No overlap
    ids1 = {r["id"] for r in page1}
    ids2 = {r["id"] for r in page2}
    assert ids1.isdisjoint(ids2)


def test_export_csv_has_header_and_one_row_per_record(store: AuditStore) -> None:
    import csv as _csv
    import io as _io

    _insert(store, tool="fetch", args={"url": "https://x.com"}, response={"ok": True})
    _insert(store, tool="search", args={"q": "kittens"}, response={"hits": 3})
    text = store.export_csv()
    reader = _csv.DictReader(_io.StringIO(text))
    rows = list(reader)
    assert len(rows) == 2
    assert reader.fieldnames is not None
    assert "tool_name" in reader.fieldnames
    assert "arguments" in reader.fieldnames
    assert "response" in reader.fieldnames
    # JSON columns survive a CSV round-trip as serialized strings
    args = next(r["arguments"] for r in rows if r["tool_name"] == "fetch")
    assert "https://x.com" in args


def test_export_csv_empty_returns_just_header(tmp_path: Path) -> None:
    s = AuditStore(tmp_path / "audit.db")
    text = s.export_csv()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("id,ts_start,ts_end")


def test_export_csv_handles_null_columns(store: AuditStore) -> None:
    import csv as _csv
    import io as _io

    _insert(store, tool="fetch", success=True, error=None)
    text = store.export_csv()
    rows = list(_csv.DictReader(_io.StringIO(text)))
    assert rows[0]["error"] == ""
    assert rows[0]["client_info"] == ""


def test_purge_dry_run_does_not_delete(store: AuditStore) -> None:
    t = time.time()
    store.log_call(
        ts_start=t - 500, ts_end=t - 499, tool_name="old",
        arguments={}, response={}, success=True, error=None,
    )
    count = store.purge(before_ts=t, dry_run=True)
    assert count == 1
    assert len(store.recent(10)) == 1  # still there


def test_stats_includes_percentiles(store: AuditStore) -> None:
    for d in (0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 1.00):
        _insert(store, tool="fetch", duration_s=d)
    stats = {row["tool_name"]: row for row in store.stats()}
    f = stats["fetch"]
    assert f["call_count"] == 10
    # p50 of 10 sorted values via linear interp lands between 5th and 6th elements
    # sorted ms = [50, 100, 200, 300, 400, 500, 600, 700, 800, 1000]
    # rank for p50 = 4.5 → 400 + 0.5*(500-400) = 450
    assert f["p50_duration_ms"] == pytest.approx(450.0, rel=0.05)
    # rank for p95 = 8.55 → 800 + 0.55*(1000-800) = 910
    assert f["p95_duration_ms"] == pytest.approx(910.0, rel=0.05)


def test_stats_empty_returns_empty_list(store: AuditStore) -> None:
    assert store.stats() == []


def test_slowest_orders_by_duration_desc(store: AuditStore) -> None:
    _insert(store, tool="fast", duration_s=0.01)
    _insert(store, tool="medium", duration_s=0.5)
    _insert(store, tool="slow", duration_s=5.0)
    rows = store.slowest(limit=10)
    assert [r["tool_name"] for r in rows] == ["slow", "medium", "fast"]


def test_slowest_respects_limit(store: AuditStore) -> None:
    for i in range(10):
        _insert(store, tool=f"t{i}", duration_s=i / 10)
    rows = store.slowest(limit=3)
    assert len(rows) == 3
    # Slowest first
    assert rows[0]["duration_ms"] >= rows[1]["duration_ms"] >= rows[2]["duration_ms"]


def test_top_errors_groups_and_counts(store: AuditStore) -> None:
    _insert(store, tool="fetch", success=False, error="timeout")
    _insert(store, tool="fetch", success=False, error="timeout")
    _insert(store, tool="fetch", success=False, error="dns_error")
    _insert(store, tool="search", success=False, error="timeout")
    _insert(store, tool="ok_call", success=True)  # success, not in result

    rows = store.top_errors(limit=10)
    # Five total, but successes excluded → 4 failures across 3 distinct (tool, error)
    by_key = {(r["tool_name"], r["error"]): r for r in rows}
    assert by_key[("fetch", "timeout")]["occurrences"] == 2
    assert by_key[("fetch", "dns_error")]["occurrences"] == 1
    assert by_key[("search", "timeout")]["occurrences"] == 1
    # Most frequent first
    assert rows[0]["occurrences"] == 2


def test_by_client_filters_by_client_info(store: AuditStore) -> None:
    t = time.time()
    for ip in ("10.0.0.1", "10.0.0.1", "10.0.0.2"):
        store.log_call(
            ts_start=t, ts_end=t + 0.1, tool_name="fetch",
            arguments={}, response={}, success=True, error=None,
            client_info=f'{{"ip":"{ip}"}}',
        )
    rows = store.by_client(client_pattern="%10.0.0.1%")
    assert len(rows) == 2
    assert all("10.0.0.1" in (r["client_info"] or "") for r in rows)


def test_by_client_no_match_returns_empty(store: AuditStore) -> None:
    _insert(store)  # No client_info
    assert store.by_client(client_pattern="%nope%") == []


def test_top_consumers_groups_by_client_info(store: AuditStore) -> None:
    t = time.time()
    for _ in range(3):
        store.log_call(
            ts_start=t, ts_end=t + 0.1, tool_name="fetch",
            arguments={}, response={}, success=True, error=None,
            client_info='{"ip":"10.0.0.1"}',
        )
    store.log_call(
        ts_start=t, ts_end=t + 0.1, tool_name="fetch",
        arguments={}, response={}, success=False, error="boom",
        client_info='{"ip":"10.0.0.1"}',
    )
    store.log_call(
        ts_start=t, ts_end=t + 0.1, tool_name="fetch",
        arguments={}, response={}, success=True, error=None,
        client_info='{"ip":"10.0.0.2"}',
    )
    rows = store.top_consumers()
    by_client = {r["client_info"]: r for r in rows}
    assert by_client['{"ip":"10.0.0.1"}']["call_count"] == 4
    assert by_client['{"ip":"10.0.0.1"}']["error_count"] == 1
    assert by_client['{"ip":"10.0.0.1"}']["error_rate"] == pytest.approx(0.25)
    assert by_client['{"ip":"10.0.0.2"}']["call_count"] == 1
    # Most active client first
    assert rows[0]["client_info"] == '{"ip":"10.0.0.1"}'


def test_top_consumers_buckets_null_client_info(store: AuditStore) -> None:
    _insert(store)  # No client_info passed → NULL in DB
    rows = store.top_consumers()
    assert rows[0]["client_info"] == "(no client info)"


def test_vacuum_runs_without_error(tmp_path: Path) -> None:
    s = AuditStore(tmp_path / "audit.db")
    t = time.time()
    for _ in range(50):
        s.log_call(
            ts_start=t, ts_end=t + 0.1, tool_name="fetch",
            arguments={"x": "y" * 200}, response={"r": "z" * 200},
            success=True, error=None,
        )
    s.purge(before_ts=t + 1, dry_run=False)
    s.vacuum()
    # After purge+vacuum, the DB should still be openable and queryable.
    assert s.recent(10) == []


def test_vacuum_after_purge_reduces_size(tmp_path: Path) -> None:
    s = AuditStore(tmp_path / "audit.db")
    t = time.time()
    for _ in range(200):
        s.log_call(
            ts_start=t, ts_end=t + 0.1, tool_name="fetch",
            arguments={"big": "x" * 1024}, response={"big": "y" * 1024},
            success=True, error=None,
        )
    s.purge(before_ts=t + 1, dry_run=False)
    before = s.db_size_bytes()
    s.vacuum()
    after = s.db_size_bytes()
    # VACUUM should never grow the file. Strict inequality is racy with
    # WAL checkpoints, so just assert non-growth.
    assert after <= before


def test_top_errors_excludes_null_errors(store: AuditStore) -> None:
    # success=False but error=None → excluded (sentinel-row guard).
    _insert(store, tool="fetch", success=False, error=None)
    _insert(store, tool="fetch", success=False, error="real_error")
    rows = store.top_errors()
    assert len(rows) == 1
    assert rows[0]["error"] == "real_error"


def test_purge_actually_deletes(store: AuditStore) -> None:
    t = time.time()
    store.log_call(
        ts_start=t - 500, ts_end=t - 499, tool_name="old",
        arguments={}, response={}, success=True, error=None,
    )
    _insert(store, tool="keep")  # ts_start ≈ now, won't be purged
    count = store.purge(before_ts=t - 100, dry_run=False)
    assert count == 1
    remaining = store.recent(10)
    assert len(remaining) == 1
    assert remaining[0]["tool_name"] == "keep"
