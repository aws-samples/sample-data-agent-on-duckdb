"""Cost policy: required partition filter + explicit path expansion."""

import json

import duckdb
import pytest
import sqlglot

from data_agent import governance
from data_agent.compute import engine
from data_agent.context import cost
from data_agent.governance import hardening
from data_agent.pipeline import run_text as run_sql

BASE = "s3://aws-public-blockchain/v1.0/eth/traces"
D1, D2, D3 = "2025-02-21", "2025-02-22", "2025-02-23"


def _paths(sql: str) -> list[str]:
    return [seg.split("/")[0] for seg in sql.split(f"'{BASE}/date=")[1:]]


# ------------------------------------------------------- required filter
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM eth_traces",
        "SELECT * FROM eth_traces WHERE value > 0 LIMIT 5",
        "SELECT * FROM eth_traces t JOIN ofac_addresses o ON lower(t.to_address) = o.address",
        "DESCRIBE eth_traces",  # binding the view lists the whole prefix too
        "SUMMARIZE eth_traces",
        f"SELECT count(*) FROM read_parquet('{BASE}/*/*.parquet', hive_partitioning=1)",
    ],
)
def test_raw_source_without_partition_predicate_is_rejected(sql):
    with pytest.raises(cost.CostRejected, match="requires a filter on its partition column `date`"):
        cost.apply(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM ws",  # user temp table
        "SELECT count(*) FROM _r_3",  # framework result table
        "SELECT count(*) FROM s3t.blockchain.eth_traces",  # catalog legs prune by metadata
        "SELECT count(*) FROM glue.blockchain.eth_traces WHERE value > 0",
        "SELECT count(*) FROM ofac_addresses",  # unpartitioned label table
        "WITH eth_traces AS (SELECT 1 AS date) SELECT * FROM eth_traces",  # a CTE of that name
        f"SELECT count(*) FROM read_parquet('{BASE}/date={D1}/*.parquet')",  # already scoped
        f"SELECT count(*) FROM read_parquet(['{BASE}/date={D1}/*.parquet'])",  # explicit list
    ],
)
def test_exempt_sources_need_no_partition_predicate(sql):
    out, info = cost.apply(sql)
    assert out == sql and info["sources"] == []


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT * FROM eth_traces WHERE date >= '{D1}'",  # unbounded → allowed, no expansion
        "SELECT * FROM eth_traces WHERE date = "
        "(SELECT max(date) FROM eth_blocks WHERE date >= '2025-02-01')",
        # filter outside the table's own scope
        f"WITH w AS (SELECT * FROM eth_traces) SELECT * FROM w WHERE date = '{D1}'",
        f"SELECT * FROM eth_traces t WHERE t.date = '{D1}' OR t.value > 1e30",  # not a pure bound
    ],
)
def test_non_literal_or_out_of_scope_filters_satisfy_the_gate_without_expansion(sql):
    out, info = cost.apply(sql)
    assert info["partition_filter"] is True and "eth_traces" in info["sources"]
    if "WITH w" in sql:
        # the CTE is transparent (SELECT *), so the outer literal filter DOES expand
        assert info["expanded_paths"] == 1
    elif "OR" not in sql:
        assert out == sql and info["expanded_paths"] == 0


# --------------------------------------------------------- path expansion
def test_equality_expands_to_one_explicit_path():
    out, info = cost.apply(f"SELECT count(*) FROM eth_traces WHERE date = '{D1}' AND value > 0")
    assert info["expanded_paths"] == 1 and _paths(out) == [D1]
    assert "*/*.parquet" not in out and "hive_partitioning" in out
    assert "AS eth_traces" in out  # the view name survives as the alias


def test_between_expands_to_each_day_inclusive():
    out, info = cost.apply(f"SELECT * FROM eth_traces WHERE date BETWEEN '{D1}' AND '{D3}'")
    assert info["expanded_paths"] == 3 and _paths(out) == [D1, D2, D3]


def test_in_list_expands_to_the_listed_days_only():
    out, _ = cost.apply(f"SELECT * FROM eth_traces WHERE date IN ('{D3}', '{D1}')")
    assert _paths(out) == [D1, D3]


def test_two_sided_comparisons_expand_with_exclusive_bounds_shifted():
    out, _ = cost.apply(f"SELECT * FROM eth_traces WHERE date > '{D1}' AND date <= '{D3}'")
    assert _paths(out) == [D2, D3]


def test_alias_qualified_predicate_is_recognised_and_alias_kept():
    out, info = cost.apply(f"SELECT t.value FROM eth_traces t WHERE t.date = '{D1}'")
    assert info["expanded_paths"] == 1 and " AS t" in out and "t.date" in out


def test_direct_whole_prefix_read_parquet_is_expanded_too():
    sql = (
        f"SELECT count(*) FROM read_parquet('{BASE}/*/*.parquet', hive_partitioning=1) r "
        f"WHERE r.date = '{D1}'"
    )
    out, info = cost.apply(sql)
    assert info["sources"] == ["eth_traces"] and _paths(out) == [D1] and " AS r" in out


def test_range_beyond_cap_is_allowed_but_not_expanded(monkeypatch):
    monkeypatch.setattr(cost, "MAX_EXPANDED_PARTITIONS", 2)
    sql = f"SELECT * FROM eth_traces WHERE date BETWEEN '{D1}' AND '{D3}'"
    out, info = cost.apply(sql)
    assert out == sql and info["partition_filter"] is True and info["expanded_paths"] == 0


def test_month_granularity_source(monkeypatch):
    monkeypatch.setattr(
        cost,
        "SOURCES",
        {
            "cur_line_items": {
                "column": "billing_period",
                "granularity": "month",
                "base": "s3://b/ops/cur",
                "layout": "{base}/billing_period={part}/*.parquet",
                "reader": "read_parquet",
                "args": "hive_partitioning=1",
            }
        },
    )
    out, info = cost.apply(
        "SELECT sum(cost) FROM cur_line_items WHERE billing_period BETWEEN '2026-07' AND '2026-09'"
    )
    assert info["expanded_paths"] == 3
    assert "billing_period=2026-07/" in out and "billing_period=2026-09/" in out


def test_governed_rewrite_is_expanded_inside_the_guard_subquery():
    """The investigator's RLS row filter names the incident window, so the
    expansion happens inside governance's guard subquery."""
    governance.set_active_role("exchange-a:investigator")
    # RLS alone is one-sided (date >= window start): filter satisfied, no expansion
    governed = governance.apply_governance("SELECT count(*) FROM eth_traces WHERE value > 0")
    assert "date >= '2025-02-21'" in governed
    out, info = cost.apply(governed)
    assert info["partition_filter"] is True and info["expanded_paths"] == 0
    # the caller's upper bound on the guard's alias composes with the RLS lower
    # bound into a closed range → three explicit day paths inside the guard
    governed = governance.apply_governance(
        f"SELECT count(*) FROM eth_traces WHERE date <= '{D3}' AND value > 0"
    )
    out, info = cost.apply(governed)
    assert info["expanded_paths"] == 3 and _paths(out) == [D1, D2, D3]
    assert "*/*.parquet" not in out and "date >= '2025-02-21'" in out  # RLS predicate kept


def test_unparseable_sql_falls_back_to_keyword_check(monkeypatch):
    def _boom(*a, **k):
        raise sqlglot.errors.ParseError("nope")

    monkeypatch.setattr(cost.sqlglot, "parse_one", _boom)
    with pytest.raises(cost.CostRejected):
        cost.apply("SELECT count(*) FROM eth_traces")
    sql = "SELECT count(*) FROM eth_traces WHERE date = '2025-02-21'"
    out, info = cost.apply(sql)
    assert info["partition_filter"] is True and out == sql  # no expansion without a parse


# ------------------------------------------ end to end on a local hive layout
@pytest.fixture()
def hive_conn(tmp_path, monkeypatch):
    base = tmp_path / "traces"
    conn = duckdb.connect()
    for day, n in ((D1, 3), (D2, 5), (D3, 7)):
        (base / f"date={day}").mkdir(parents=True)
        conn.execute(
            f"COPY (SELECT range AS id FROM range({n})) "
            f"TO '{base}/date={day}/part-0.parquet' (FORMAT parquet)"
        )
    conn.execute(
        f"CREATE VIEW tt AS SELECT * FROM read_parquet('{base}/*/*.parquet', hive_partitioning=1)"
    )
    monkeypatch.setattr(engine, "get_conn", lambda: conn)
    monkeypatch.setattr(
        cost,
        "SOURCES",
        {
            "tt": {
                "column": "date",
                "granularity": "day",
                "base": str(base),
                "layout": "{base}/date={part}/*.parquet",
                "reader": "read_parquet",
                "args": "hive_partitioning=1",
            }
        },
    )
    monkeypatch.setattr(hardening, "RAW_PATH_SCHEMES", ("s3://", str(tmp_path)))
    yield conn
    conn.close()


def test_expanded_statement_executes_and_returns_the_right_rows(hive_conn):
    out = run_sql(f"SELECT count(*) AS n FROM tt WHERE date BETWEEN '{D1}' AND '{D2}'")
    rows = json.loads(out.split("\n")[0])
    assert rows == [{"n": "8"}]  # 3 + 5, day 3 never listed


def test_gate_rejection_reaches_the_model_as_an_actionable_error(hive_conn):
    out = run_sql("SELECT count(*) FROM tt")
    assert out.startswith(
        "ERROR: cost policy: scanning `tt` requires a filter on its partition column `date`"
    )


def test_streaming_tool_emits_a_cost_phase_only_for_partitioned_sources(hive_conn):
    import asyncio

    import agent as agent_mod

    async def _collect(sql):
        return [e async for e in agent_mod.run_sql._tool_func(sql)]

    events = asyncio.run(_collect(f"SELECT count(*) FROM tt WHERE date = '{D1}'"))
    phases = [e["phase"] for e in events if isinstance(e, dict)]
    assert phases == ["gate", "rewrite", "cost", "execute"]
    c = next(e for e in events if isinstance(e, dict) and e.get("phase") == "cost")
    assert (
        c["sources"] == ["tt"]
        and c["expanded_paths"] == 1
        and "date=2025-02-21" in c["rewritten_sql"]
    )
    plain = asyncio.run(_collect("SELECT 1"))
    assert [e["phase"] for e in plain if isinstance(e, dict)] == ["gate", "rewrite", "execute"]
    rejected = asyncio.run(_collect("SELECT count(*) FROM tt"))
    assert [e["phase"] for e in rejected if isinstance(e, dict)] == ["gate", "rewrite", "cost"]
    assert rejected[-1].startswith("ERROR: cost policy")
