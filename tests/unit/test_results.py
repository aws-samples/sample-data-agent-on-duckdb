"""Result shaping: materialize, summarize, read back — never truncate."""

import json

import pytest

import agent as agent_mod
from data_agent import governance
from data_agent.context import results
from data_agent.pipeline import run_text as run_sql

JUNIOR = "exchange-a:junior"


def _lines(out: str) -> tuple[dict | list, dict, str | None]:
    parts = out.split("\n")
    return json.loads(parts[0]), json.loads(parts[1]), (parts[2] if len(parts) > 2 else None)


# ------------------------------------------------------------ inline path
def test_small_result_inline_and_leaves_no_table_behind(local_conn):
    out = run_sql("SELECT id FROM t WHERE id < 3 ORDER BY id")
    rows, metrics, caveat = _lines(out)
    assert rows == [{"id": "0"}, {"id": "1"}, {"id": "2"}]
    assert metrics["shape"] == "rows" and "ref" not in metrics
    assert caveat is None
    assert results.STORE.refs() == []
    assert (
        local_conn.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE '\\_r\\_%' ESCAPE '\\'"
        ).fetchone()[0]
        == 0
    )


def test_inline_result_preserves_order(local_conn):
    rows, _, _ = _lines(run_sql("SELECT id FROM t WHERE id < 5 ORDER BY id DESC"))
    assert [r["id"] for r in rows] == ["4", "3", "2", "1", "0"]


# ----------------------------------------------------------- summary path
def test_row_overflow_yields_summary_with_retrieval_path(local_conn):
    summary, metrics, caveat = _lines(run_sql("SELECT id, name FROM t"))
    assert summary["shape"] == "summary" and summary["rows"] == 500 and summary["rows_exact"]
    ref = summary["ref"]
    assert ref == "_r_1"
    assert summary["columns"] == [
        {"name": "id", "type": "BIGINT"},
        {"name": "name", "type": "VARCHAR"},
    ]
    assert len(summary["sample"]) == results.SAMPLE_ROWS
    assert all(ref in q for q in summary["read_with"])
    assert metrics["ref"] == ref and metrics["rows"] == 500
    assert caveat.startswith("[Result not shown inline: 500 rows.") and ref in caveat
    assert "sample, not the result" in caveat


def test_char_overflow_yields_summary_even_when_rows_are_few(local_conn, monkeypatch):
    monkeypatch.setattr(results, "MAX_RESULT_CHARS", 200)
    summary, _, _ = _lines(run_sql("SELECT id, repeat('x', 50) AS wide FROM t WHERE id < 10"))
    assert summary["shape"] == "summary" and summary["rows"] == 10


def test_materialized_table_preserves_order_for_read_back(local_conn):
    summary, _, _ = _lines(run_sql("SELECT id FROM t ORDER BY id DESC"))
    rows, _, _ = _lines(run_sql(f"SELECT id FROM {summary['ref']} LIMIT 3"))
    assert [r["id"] for r in rows] == ["499", "498", "497"]


def test_sample_values_are_shortened_but_table_is_whole(local_conn):
    summary, _, _ = _lines(run_sql("SELECT id, repeat('y', 500) AS blob FROM t"))
    assert summary["sample"][0]["blob"].endswith("…")
    assert len(summary["sample"][0]["blob"]) == results.SAMPLE_VALUE_CHARS
    full = local_conn.execute(f"SELECT max(length(blob)) FROM {summary['ref']}").fetchone()[0]
    assert full == 500


# ------------------------------------------------------- materialize cap
def test_materialize_cap_marks_inexact_and_keeps_first_rows(local_conn, monkeypatch):
    monkeypatch.setattr(results, "MAX_MATERIALIZE_ROWS", 10)
    summary, _, caveat = _lines(run_sql("SELECT id FROM t ORDER BY id"))
    assert summary["rows_exact"] is False and summary["rows"] == 11  # cap+1 signals overflow
    assert "more than 10 rows" in caveat
    assert local_conn.execute(f"SELECT max(id) FROM {summary['ref']}").fetchone()[0] == 10


@pytest.mark.parametrize(
    "sql, expected",
    [
        (
            "SELECT id FROM t ORDER BY id",
            "SELECT id FROM t ORDER BY id\nLIMIT 11",
        ),  # no LIMIT → append
        ("SELECT id FROM t LIMIT 5", "SELECT id FROM t LIMIT 5"),  # small LIMIT → untouched
        (
            "SELECT id FROM t LIMIT 500;",
            "SELECT * FROM (\nSELECT id FROM t LIMIT 500\n) AS _q LIMIT 11",
        ),
        (
            "SELECT id FROM t LIMIT (SELECT 3)",
            "SELECT * FROM (\nSELECT id FROM t LIMIT (SELECT 3)\n) AS _q LIMIT 11",
        ),
        (
            "SELECT id FROM t WHERE id < 3 UNION ALL SELECT id FROM t WHERE id > 497",
            "SELECT id FROM t WHERE id < 3 UNION ALL SELECT id FROM t WHERE id > 497\nLIMIT 11",
        ),
    ],
)
def test_capped_sql_shapes(sql, expected):
    assert results.capped(sql, 11) == expected


def test_capped_falls_back_to_wrapper_when_sqlglot_cannot_parse(monkeypatch):
    monkeypatch.setattr(results, "_existing_limit", lambda sql: (False, None))
    assert results.capped("SELECT 1", 7) == "SELECT * FROM (\nSELECT 1\n) AS _q LIMIT 7"


# ------------------------------------------------------------ LRU + refs
def test_lru_evicts_least_recently_queried_table(local_conn, monkeypatch):
    monkeypatch.setattr(results, "RESULT_TABLE_LRU", 2)
    r1 = _lines(run_sql("SELECT id FROM t"))[0]["ref"]
    r2 = _lines(run_sql("SELECT name FROM t"))[0]["ref"]
    run_sql(f"SELECT count(*) FROM {r1}")  # touch r1 → r2 is now the coldest
    summary, metrics, caveat = _lines(run_sql("SELECT id, name FROM t"))
    r3 = summary["ref"]
    assert metrics["evicted"] == [r2] and r2 in caveat
    assert results.STORE.refs() == [r1, r3]
    assert run_sql(f"SELECT count(*) FROM {r2}").startswith("ERROR")  # engine: gone
    assert '"n": "500"' in run_sql(f"SELECT count(*) AS n FROM {r1}")


def test_refs_are_monotonic_across_inline_and_summary_results(local_conn):
    _lines(run_sql("SELECT id FROM t WHERE id < 2"))  # inline: ref consumed, table dropped
    summary, _, _ = _lines(run_sql("SELECT id FROM t"))
    assert summary["ref"] == "_r_2"


# ------------------------------------------------- reserved prefix (gate)
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TEMP TABLE _r_1 AS SELECT 1",
        "CREATE OR REPLACE TEMPORARY VIEW _r_9 AS SELECT 1",
        "WITH _r_2 AS (SELECT 1) SELECT * FROM _r_2",
        "WITH a AS (SELECT 1), _r_3 AS (SELECT 2) SELECT * FROM a",
        "SELECT * FROM t AS _r_4",
        "SELECT * FROM (SELECT 1) AS _r_5",
    ],
)
def test_defining_or_aliasing_a_reserved_name_is_rejected(sql, local_conn):
    out = run_sql(sql)
    assert out.startswith("ERROR: names starting with _r_ are reserved")


def test_reading_a_reserved_name_is_allowed(local_conn):
    ref = _lines(run_sql("SELECT id FROM t"))[0]["ref"]
    assert not run_sql(f"SELECT id FROM {ref} WHERE id = 7").startswith("ERROR")


# ------------------------------------------ governance × materialization
THREE_TENANT = (
    "CREATE TABLE cex_deposit_labels AS SELECT * FROM (VALUES "
    "('exchange-a', 'cust-A1', '0xa1', 'a1'), ('exchange-b', 'cust-B1', '0xb1', 'b1'), "
    "('exchange-c', 'cust-C1', '0xc1', 'c1')) v(exchange, subject_identity, address, label)"
)


def test_materialized_result_holds_only_governed_rows_and_columns(local_conn, monkeypatch):
    """The temp table is built from the GOVERNED statement, so later plain
    reads of _r_<n> cannot widen access."""
    local_conn.execute(THREE_TENANT)
    monkeypatch.setattr(results, "MAX_RESULT_ROWS", 0)  # force the summary path
    governance.set_active_role(JUNIOR)
    summary, metrics, _ = _lines(run_sql("SELECT * FROM cex_deposit_labels"))
    assert metrics["governed"] is True and summary["rows"] == 1
    assert "subject_identity" not in [c["name"] for c in summary["columns"]]
    rows = local_conn.execute(f"SELECT * FROM {summary['ref']}").fetchall()
    assert rows == [("exchange-a", "0xa1", "a1")]


def test_metadata_statements_take_the_direct_path(local_conn):
    out = run_sql("DESCRIBE t")
    rows, metrics, _ = _lines(out)
    assert metrics["shape"] == "rows" and [r["column_name"] for r in rows] == ["id", "name"]
    assert results.STORE.refs() == []


# ----------------------------------------------------------- streaming
def test_streaming_execute_phase_reports_shape_and_ref(local_conn):
    import asyncio

    async def _collect():
        return [e async for e in agent_mod.run_sql._tool_func("SELECT id FROM t")]

    events = asyncio.run(_collect())
    execute = next(e for e in events if isinstance(e, dict) and e.get("phase") == "execute")
    assert execute["shape"] == "summary" and execute["materialized"] == "_r_1"
    assert execute["rows"] == 500 and execute["evicted"] == []
    assert json.loads(events[-1].split("\n")[0])["ref"] == "_r_1"
