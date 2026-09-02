"""Unit tests for the run_sql tool gating/formatting and the data catalog.

No network, no Bedrock, no S3: the DuckDB connection is replaced with a local
in-memory one via monkeypatching get_conn.
"""

import json

import duckdb
import pytest

import agent as agent_mod
from agent import run_sql
from data_catalog import CATALOG_DOC, S3_BASE, VIEW_DEFS, build_catalog_doc


@pytest.fixture()
def local_conn(monkeypatch):
    """Swap the S3-backed connection for a plain in-memory DuckDB."""
    conn = duckdb.connect()
    conn.execute("CREATE TABLE t AS SELECT range AS id, 'x' || range AS name FROM range(500)")
    monkeypatch.setattr(agent_mod, "get_conn", lambda: conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------- gating
@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t",
        "DELETE FROM t",
        "INSERT INTO t VALUES (1, 'a')",
        "UPDATE t SET name = 'b'",
        "CREATE TABLE evil (x int)",
        "CREATE TABLE evil AS SELECT 1",  # non-temp CREATE ... AS SELECT
        "CREATE OR REPLACE TABLE evil AS SELECT 1",
        "ATTACH 'other.db'",
        "COPY t TO '/tmp/out.csv'",
        "SET memory_limit='999GB'",
    ],
)
def test_non_select_statements_rejected(sql):
    out = run_sql(sql)
    assert out.startswith("ERROR: only")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "  select 1",
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
        "DESCRIBE SELECT 1 AS a",
        "SHOW TABLES",
    ],
)
def test_read_statements_allowed(sql, local_conn):
    out = run_sql(sql)
    assert not out.startswith("ERROR: only SELECT")


# ---------------------------------------------------------------- formatting
def test_result_is_json_with_column_names(local_conn):
    out = run_sql("SELECT id, name FROM t WHERE id < 3 ORDER BY id")
    rows = json.loads(out.split("\n")[0])  # line 1 = rows, line 2 = metrics
    assert rows == [
        {"id": "0", "name": "x0"},
        {"id": "1", "name": "x1"},
        {"id": "2", "name": "x2"},
    ]


def test_truncation_at_row_cap(local_conn):
    out = run_sql("SELECT * FROM t")
    assert "[TRUNCATED at 200 rows" in out
    assert len(json.loads(out.split("\n")[0])) == 200


def test_engine_error_returned_verbatim_for_model_repair(local_conn):
    out = run_sql("SELECT nonexistent_col FROM t")
    assert out.startswith("ERROR: ")
    assert "nonexistent_col" in out


# ------------------------------------------------- temp tables (session state)
def test_temp_table_roundtrip_and_visibility_across_calls(local_conn):
    created = run_sql("CREATE TEMP TABLE ws AS SELECT * FROM t WHERE id < 10")
    assert not created.startswith("ERROR")
    out = run_sql("SELECT count(*) AS n FROM ws")  # separate run_sql call
    assert '"n": "10"' in out


def test_temp_view_allowed(local_conn):
    assert not run_sql("CREATE TEMP VIEW v AS SELECT id FROM t").startswith("ERROR")
    assert not run_sql("CREATE OR REPLACE TEMPORARY TABLE ws2 AS SELECT 1 AS a").startswith("ERROR")


# ---------------------------------------------------------------- metrics
def test_engine_metrics_appended(local_conn):
    out = run_sql("SELECT id FROM t WHERE id < 3")
    lines = out.split("\n")
    metrics = json.loads(lines[1])
    assert metrics["rows"] == 3
    assert isinstance(metrics["engine_ms"], (int, float)) and metrics["engine_ms"] >= 0


def test_metrics_present_even_when_truncated(local_conn):
    out = run_sql("SELECT * FROM t")
    assert '"engine_ms"' in out
    assert "[TRUNCATED at 200 rows" in out


# ---------------------------------------------------------------- catalog
def test_view_defs_point_at_public_dataset_with_hive_partitioning():
    assert set(VIEW_DEFS) == {
        "btc_blocks",
        "btc_transactions",
        "eth_blocks",
        "eth_transactions",
        "eth_token_transfers",
        "eth_logs",
    }
    for src in VIEW_DEFS.values():
        assert src.startswith("read_parquet('s3://aws-public-blockchain/")
        assert "hive_partitioning=1" in src


def test_catalog_doc_teaches_date_filter_discipline():
    assert S3_BASE in CATALOG_DOC
    assert "ALWAYS filter on `date`" in CATALOG_DOC


# ------------------------------------------------------------- catalog legs
def test_active_legs_follow_env_toggles(monkeypatch):
    monkeypatch.setattr(agent_mod, "S3_TABLES_ARN", "")
    monkeypatch.setattr(agent_mod, "GLUE_CATALOG", "")
    monkeypatch.setattr(agent_mod, "DUCKLAKE_CATALOG", "")
    assert agent_mod.active_legs() == []
    monkeypatch.setattr(agent_mod, "S3_TABLES_ARN", "arn:aws:s3tables:x")
    monkeypatch.setattr(agent_mod, "DUCKLAKE_CATALOG", "s3://b/cat.ducklake")
    assert agent_mod.active_legs() == ["s3t", "dl"]
    monkeypatch.setattr(agent_mod, "GLUE_CATALOG", "123456789012:s3tablescatalog/tb")
    assert agent_mod.active_legs() == ["s3t", "glue", "dl"]


def test_catalog_doc_raw_only_omits_leg_sections():
    doc = build_catalog_doc([])
    assert "Catalog-based access methods" not in doc
    assert "s3t.blockchain" not in doc
    assert "Which access path to use" not in doc


def test_catalog_doc_lists_only_active_legs():
    doc = build_catalog_doc(["s3t", "dl"])
    assert "`s3t` — S3 Tables native Iceberg REST catalog" in doc
    assert "`dl` — DuckLake" in doc
    assert "Glue Data Catalog" not in doc  # inactive leg must not leak into prompt


def test_catalog_doc_full_spectrum_has_scenario_table():
    doc = build_catalog_doc(["s3t", "glue", "dl"])
    assert "Which access path to use" in doc
    # one scenario row per access path, raw first
    for marker in ("| raw views", "| `s3t.*` |", "| `glue.*` |", "| `dl.*` |"):
        assert marker in doc
    # glue and s3t are explicitly the same physical table
    assert "THE SAME physical table" in doc


def test_parallel_run_sql_calls_do_not_steal_results(local_conn):
    """Strands runs same-message tool calls on threads; the shared DuckDB
    connection must be serialized or one call fetches the other's rows
    (seen in cloud as count(*) -> 0 rows)."""
    import threading

    outs = {}

    def worker(i):
        outs[i] = run_sql(f"SELECT count(*) AS n FROM t WHERE id < {100 + i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i, out in outs.items():
        assert f'"n": "{100 + i}"' in out, f"call {i} lost its result: {out[:80]}"


def test_attach_legs_mounts_ducklake_readonly(monkeypatch, tmp_path):
    """dl leg: real ATTACH against a local ducklake catalog, no network."""
    src = duckdb.connect()
    src.execute(f"ATTACH 'ducklake:{tmp_path}/cat.ducklake' AS dl (DATA_PATH '{tmp_path}/data')")
    src.execute("CREATE TABLE dl.t AS SELECT 42 AS x")
    src.close()
    monkeypatch.setattr(agent_mod, "S3_TABLES_ARN", "")
    monkeypatch.setattr(agent_mod, "GLUE_CATALOG", "")
    monkeypatch.setattr(agent_mod, "DUCKLAKE_CATALOG", f"{tmp_path}/cat.ducklake")
    conn = duckdb.connect()
    agent_mod._attach_legs(conn)
    assert conn.execute("SELECT x FROM dl.main.t").fetchone() == (42,)
    with pytest.raises(duckdb.Error):  # READ_ONLY mount rejects writes
        conn.execute("INSERT INTO dl.main.t VALUES (1)")
    conn.close()
