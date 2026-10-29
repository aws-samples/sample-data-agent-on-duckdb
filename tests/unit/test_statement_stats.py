"""Per-statement evidence — bytes the engine read and the HTTP requests
httpfs made — collected from the engine's own instrumentation (JSON profiling
TOTAL_BYTES_READ + the HTTP log), zero network, and carried on the execute
trace event and the audit line. A local-table statement correctly reports
0 requests: that is the marginal-cost story the numbers exist to tell."""

import json

import duckdb
import pytest

from data_agent import audit, pipeline
from data_agent.compute import engine


@pytest.fixture()
def _fresh_stats(monkeypatch):
    """Isolate the module-global enablement flag per test."""
    monkeypatch.setitem(engine._stats, "enabled", False)
    yield


def test_a_local_file_scan_reports_bytes_and_zero_requests(tmp_path, _fresh_stats):
    conn = duckdb.connect()
    conn.execute("SET home_directory='/tmp'")
    pq = tmp_path / "t.parquet"
    conn.execute(
        f"COPY (SELECT range AS i, 'x' || range AS s FROM range(10000)) TO '{pq}' (FORMAT parquet)"
    )
    engine.enable_statement_stats(conn)
    assert engine._stats["enabled"]
    engine.begin_statement_stats(conn)
    conn.execute(f"SELECT sum(i) FROM read_parquet('{pq}')").fetchall()
    stats = engine.collect_statement_stats(conn)
    assert stats["bytes_scanned"] and stats["bytes_scanned"] > 0
    assert stats["s3_requests"] == 0
    conn.close()


def test_stats_reset_between_statements(tmp_path, _fresh_stats):
    conn = duckdb.connect()
    conn.execute("SET home_directory='/tmp'")
    pq = tmp_path / "t.parquet"
    conn.execute(f"COPY (SELECT range AS i FROM range(1000)) TO '{pq}' (FORMAT parquet)")
    engine.enable_statement_stats(conn)
    engine.begin_statement_stats(conn)
    conn.execute(f"SELECT count(*) FROM read_parquet('{pq}')").fetchall()
    first = engine.collect_statement_stats(conn)
    engine.begin_statement_stats(conn)
    conn.execute("SELECT 1").fetchall()
    second = engine.collect_statement_stats(conn)
    assert first["bytes_scanned"] > second["bytes_scanned"] == 0
    conn.close()


def test_a_build_without_the_instrumentation_reports_none(_fresh_stats):
    conn = duckdb.connect()
    assert engine._stats["enabled"] is False
    engine.begin_statement_stats(conn)  # must be a no-op, not an error
    assert engine.collect_statement_stats(conn) == {"bytes_scanned": None, "s3_requests": None}
    conn.close()


def test_the_pipeline_carries_the_evidence_to_trace_and_audit(local_conn, capsys, _fresh_stats):
    # the hooks are wired at pipeline import; enabling stats on the test
    # connection is what the real engine init does in _configure
    engine.enable_statement_stats(local_conn)
    audit.reset_session()
    items = list(pipeline.run("SELECT count(*) AS n FROM t"))
    ex = next(e for e in items if isinstance(e, dict) and e.get("phase") == "execute")
    assert ex["s3_requests"] == 0 and ex["bytes_scanned"] is not None
    line = json.loads([ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln][-1])
    assert line["audit"]["s3_requests"] == 0
    assert line["S3Requests"] == 0 and "BytesScanned" in line
    names = [m["Name"] for m in line["_aws"]["CloudWatchMetrics"][0]["Metrics"]]
    assert "BytesScanned" in names and "S3Requests" in names
