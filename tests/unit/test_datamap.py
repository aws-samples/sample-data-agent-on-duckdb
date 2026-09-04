"""Data map: L0 generated from the engine catalog, L1 zero-network search."""

import json
import threading

import agent as agent_mod
from data_agent.context import datamap, prompt

LOCK = threading.Lock()


def _scenario(conn):
    """Local stand-ins with the shapes of the real catalog."""
    conn.execute(
        "CREATE VIEW eth_traces AS SELECT '0xa' AS from_address, '0xb' AS to_address, "
        "1 AS value, '2025-02-21' AS date"
    )
    conn.execute(
        "CREATE VIEW cex_deposit_labels AS SELECT 'exchange-a' AS exchange, "
        "'c1' AS subject_identity, '0xa' AS address, 'hot' AS risk_tier"
    )
    conn.execute("CREATE VIEW ofac_addresses AS SELECT '0xa' AS address, 'CYBER' AS program")
    conn.execute("CREATE TEMP TABLE _r_1 AS SELECT 1 AS x")  # framework result table
    conn.execute("CREATE TEMP TABLE ws AS SELECT 1 AS x")  # user working set


# -------------------------------------------------------------------- L0
def test_entries_list_durable_sources_and_hide_framework_and_temp_objects(local_conn):
    _scenario(local_conn)
    names = {e["name"]: e for e in datamap.catalog_entries(local_conn)}
    assert {"t", "eth_traces", "cex_deposit_labels", "ofac_addresses"} <= set(names)
    assert "_r_1" not in names and "ws" not in names and "_catalog" not in names
    assert names["eth_traces"]["partition"] == "date"  # from PARTITIONED_SOURCES
    assert names["ofac_addresses"]["partition"] is None
    assert names["t"]["kind"] == "table" and names["eth_traces"]["kind"] == "view"


def test_probe_for_a_partitioned_source_uses_a_single_partition_path(local_conn, monkeypatch):
    monkeypatch.setattr(datamap, "PROBE_REMOTE", True)  # the deployed default
    _scenario(local_conn)
    e = next(e for e in datamap.catalog_entries(local_conn) if e["name"] == "eth_traces")
    assert "date=2025-02-21/*.parquet" in e["probe"] and "*/*.parquet" not in e["probe"]
    assert e["probe"].startswith("DESCRIBE SELECT * FROM read_parquet(")


def test_l0_doc_is_a_compact_table_with_partition_columns(local_conn):
    _scenario(local_conn)
    doc = datamap.l0_doc(datamap.catalog_entries(local_conn))
    assert "| `eth_traces` | view | `date` |" in doc
    assert "| `ofac_addresses` | view | —" in doc
    assert "search_schema" in doc and "DESCRIBE" in doc
    assert doc.count("\n") < 20  # names only: stays small


def test_prompt_template_gets_the_l0_block_injected(local_conn):
    _scenario(local_conn)
    assert "__L0__" not in prompt.SYSTEM_PROMPT
    l0 = prompt.l0_block()
    assert l0 and "`eth_traces`" in l0
    text = prompt.build(l0)
    assert "Tables the engine can see" in text and "__L0__" not in text
    assert text.startswith("You are a data investigator with an in-process analytics engine")


# -------------------------------------------------------------------- L1
def test_search_ranks_by_name_and_column_tokens_with_fts(local_conn):
    _scenario(local_conn)
    hits = datamap.search(local_conn, LOCK, "sanctions program", limit=3)
    assert hits and hits[0]["table"] == "ofac_addresses"
    assert hits[0]["engine"].startswith("duckdb fts")
    hits = datamap.search(local_conn, LOCK, "deposit exchange risk", limit=3)
    assert hits[0]["table"] == "cex_deposit_labels"
    assert "subject_identity" in hits[0]["columns"]
    hits = datamap.search(local_conn, LOCK, "traces", limit=3)
    assert hits[0]["table"] == "eth_traces" and hits[0]["partition_column"] == "date"


def test_index_is_built_once_and_hidden_from_l0(local_conn):
    _scenario(local_conn)
    datamap.search(local_conn, LOCK, "traces")
    assert datamap._state["built"] is True
    assert "_catalog" not in {e["name"] for e in datamap.catalog_entries(local_conn)}
    n = local_conn.execute("SELECT count(*) FROM _catalog").fetchone()[0]
    datamap.search(local_conn, LOCK, "labels")  # no rebuild
    assert local_conn.execute("SELECT count(*) FROM _catalog").fetchone()[0] == n


def test_token_overlap_fallback_when_fts_is_unavailable(local_conn):
    _scenario(local_conn)
    datamap.ensure_index(local_conn, LOCK)
    datamap._state["fts"] = False  # simulate: extension could not load
    hits = datamap.search(local_conn, LOCK, "ofac address", limit=3)
    assert hits[0]["table"] == "ofac_addresses" and "fallback" in hits[0]["engine"]
    assert hits[0]["score"] >= 2


def test_search_with_no_match_or_empty_query_is_empty(local_conn):
    _scenario(local_conn)
    assert datamap.search(local_conn, LOCK, "quarterly revenue forecast") == []
    assert datamap.search(local_conn, LOCK, "  ") == []


def test_search_schema_tool_returns_json_hits_and_is_registered(local_conn):
    _scenario(local_conn)
    out = json.loads(agent_mod.search_schema._tool_func("deposit labels", 5))
    assert out["query"] == "deposit labels"
    assert out["hits"][0]["table"] == "cex_deposit_labels"
    assert set(agent_mod.agent.tool_names) >= {"run_sql", "search_schema"}


def test_search_schema_tool_never_raises(local_conn, monkeypatch):
    monkeypatch.setattr(
        datamap, "search", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    out = json.loads(agent_mod.search_schema._tool_func("x", 5))
    assert out["hits"] == [] and "boom" in out["error"]
