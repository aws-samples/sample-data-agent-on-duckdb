"""Unit tests for the run_sql tool gating/formatting and the data catalog.

No network, no Bedrock, no S3: the DuckDB connection is replaced with a local
in-memory one via monkeypatching get_conn.
"""

import json

import duckdb
import pytest

import agent as agent_mod
import governance
from agent import _run_sql_core as run_sql
from data_catalog import (
    CATALOG_DOC,
    S3_BASE,
    VIEW_DEFS,
    build_catalog_doc,
    build_label_views,
    validate_labels_prefix,
)

UNRESTRICTED = "ops:admin"


@pytest.fixture(autouse=True)
def _reset_governance():
    """Every test starts from the unrestricted admin role — set EXPLICITLY. The
    module default is the deny-all floor (see docs/design.md), so relying on
    DEFAULT_ROLE here would make every plain SQL test fail closed."""
    governance.set_active_role(UNRESTRICTED)
    yield
    governance.set_active_role(UNRESTRICTED)


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


@pytest.mark.parametrize(
    "sql",
    [
        "-- a leading comment\nSELECT 1",
        "-- line one\n-- line two\nselect 1",
        "/* block comment */ SELECT 1",
        "/* multi\nline */\nSELECT 1",
        "   -- indented comment\n   CREATE TEMP TABLE ws AS SELECT 1 AS a",
    ],
)
def test_read_statements_allowed_despite_leading_comments(sql, local_conn):
    """Models routinely prefix SQL with a comment; the gate must classify on
    the first real keyword, not the comment."""
    out = run_sql(sql)
    assert not out.startswith("ERROR: only")


def test_leading_comment_does_not_smuggle_a_write():
    """Stripping leading comments must not let a write through."""
    out = run_sql("-- harmless looking\nDROP TABLE t")
    assert out.startswith("ERROR: only")


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
    assert isinstance(metrics["engine_ms"], int | float) and metrics["engine_ms"] >= 0


def test_metrics_present_even_when_truncated(local_conn):
    out = run_sql("SELECT * FROM t")
    assert '"engine_ms"' in out
    assert "[TRUNCATED at 200 rows" in out


# ---------------------------------------------------------------- catalog
def test_view_defs_point_at_public_dataset_with_hive_partitioning():
    assert set(VIEW_DEFS) == {
        "eth_blocks",
        "eth_transactions",
        "eth_token_transfers",
        "eth_traces",
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


def test_catalog_doc_label_section_gated_by_flag():
    assert "Attribution label tables" not in build_catalog_doc([])
    doc = build_catalog_doc([], labels=True)
    assert "Attribution label tables" in doc
    assert "ofac_addresses" in doc and "cex_deposit_labels" in doc


def test_build_label_views_off_when_prefix_empty():
    assert build_label_views("") == {}
    views = build_label_views("s3://bucket/labels", frozenset({"bucket"}))
    assert set(views) == {"ofac_addresses", "cex_deposit_labels"}
    assert views["ofac_addresses"] == "read_parquet('s3://bucket/labels/ofac_addresses.parquet')"


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


# --------------------------------------------------------------- governance
INVESTIGATOR = "exchange-a:investigator"
JUNIOR = "exchange-a:junior"
CASE_WINDOW = "date >= '2025-02-21'"


def test_resolve_role_two_stage_mapping():
    assert governance.resolve_role({"tenant": "exchange-a", "role": "investigator"}) == INVESTIGATOR
    assert governance.resolve_role({"tenant": "ops", "role": "admin"}) == "ops:admin"
    # unknown role within known tenant → that tenant's policy (first match), not admin
    assert governance.resolve_role({"tenant": "exchange-a", "role": "nobody"}).startswith(
        "exchange-a:"
    )
    # unknown tenant / missing claims → default role, which is the deny-all floor
    assert governance.resolve_role({"tenant": "nobody", "role": "x"}) == governance.DEFAULT_ROLE
    assert governance.resolve_role(None) == governance.DEFAULT_ROLE
    assert governance.DEFAULT_ROLE == governance.DENY_ALL_ROLE


def test_unrestricted_role_passes_sql_through():
    sql = "SELECT count(*) FROM eth_traces WHERE date='2025-02-21'"
    assert governance.apply_governance(sql) == sql


def test_rls_predicate_injected_for_restricted_role():
    governance.set_active_role(INVESTIGATOR)
    out = governance.apply_governance("SELECT count(*) FROM eth_traces")
    assert CASE_WINDOW in out


def test_cls_denies_subject_identity_for_junior_only():
    governance.set_active_role(JUNIOR)
    out = governance.apply_governance("SELECT * FROM cex_deposit_labels")
    assert "EXCLUDE (subject_identity)" in out
    assert "exchange = 'exchange-a'" in out
    # investigator sees the same rows but NOT the column exclusion
    governance.set_active_role(INVESTIGATOR)
    out2 = governance.apply_governance("SELECT * FROM cex_deposit_labels")
    assert "EXCLUDE" not in out2
    assert "exchange = 'exchange-a'" in out2


def test_rls_applies_to_every_catalog_access_method():
    governance.set_active_role(INVESTIGATOR)
    for ref in (
        "eth_traces",
        "s3t.blockchain.eth_traces",
        "glue.blockchain.eth_traces",
        "dl.blockchain.eth_traces",
    ):
        out = governance.apply_governance(f"SELECT count(*) FROM {ref}")
        assert CASE_WINDOW in out, ref
        assert ref in out, "original catalog-qualified reference must survive"


def test_rls_preserves_alias_in_joins():
    governance.set_active_role(INVESTIGATOR)
    out = governance.apply_governance(
        "SELECT t.transaction_hash FROM s3t.blockchain.eth_traces t "
        "JOIN eth_blocks b ON t.block_number = b.number"
    )
    assert "AS t" in out and CASE_WINDOW in out
    assert "eth_blocks" in out  # ungoverned table untouched


def test_rls_applies_inside_cte_and_ctas_but_not_to_local_names():
    governance.set_active_role(INVESTIGATOR)
    out = governance.apply_governance("WITH w AS (SELECT * FROM eth_traces) SELECT count(*) FROM w")
    assert out.count(CASE_WINDOW) == 1  # base table guarded, CTE name not
    out2 = governance.apply_governance(
        "CREATE TEMP TABLE case_flows AS SELECT * FROM eth_traces WHERE date='2025-02-21'"
    )
    assert CASE_WINDOW in out2 and out2.lower().startswith("create")


def test_raw_path_access_rejected_for_restricted_role():
    governance.set_active_role(INVESTIGATOR)
    with pytest.raises(ValueError, match="raw file access"):
        governance.apply_governance(
            "SELECT count(*) FROM read_parquet('s3://aws-public-blockchain/x/*.parquet')"
        )


def test_governance_fails_closed_on_unparseable_sql():
    governance.set_active_role(INVESTIGATOR)
    with pytest.raises(ValueError, match="could not be parsed"):
        governance.apply_governance("SELECT ??? FROM !!!")


def test_denied_column_explicit_select_is_cleanly_rejected(local_conn):
    """End-to-end through run_sql: the junior principal that explicitly names a
    denied column gets a clean governance rejection naming the column + role
    (not an opaque engine binder error), and never the data."""
    local_conn.execute(
        "CREATE TABLE cex_deposit_labels AS "
        "SELECT 'exchange-a' AS exchange, 'customer#1' AS subject_identity, '0xabc' AS address"
    )
    governance.set_active_role(JUNIOR)
    out = run_sql("SELECT subject_identity FROM cex_deposit_labels")
    assert out.startswith("ERROR:")
    assert "subject_identity" in out and "not visible" in out
    assert JUNIOR in out
    assert "customer#1" not in out  # the value never leaks
    # a non-denied column on the same governed table still works
    ok = run_sql("SELECT address FROM cex_deposit_labels")
    assert '"address": "0xabc"' in ok
    assert '"governed": true' in ok


def test_investigator_may_select_the_identity_column(local_conn):
    """The same explicit column is allowed for the investigator role — proving
    the denial is role-specific, the crux of the governance demo."""
    local_conn.execute(
        "CREATE TABLE cex_deposit_labels AS "
        "SELECT 'exchange-a' AS exchange, 'customer#1' AS subject_identity, '0xabc' AS address"
    )
    governance.set_active_role(INVESTIGATOR)
    out = run_sql("SELECT subject_identity FROM cex_deposit_labels")
    assert '"subject_identity": "customer#1"' in out
    assert '"governed": true' in out


def test_governed_flag_false_for_unrestricted(local_conn):
    out = run_sql("SELECT count(*) AS n FROM t")
    assert '"governed": false' in out


# ═══════════════════════════════════════════════════════════════════════════
# Code validation, round 1: three fail-open defects in the authorization path
# ═══════════════════════════════════════════════════════════════════════════


# ---------------- 1. read-only gate must apply to the WHOLE submission -------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE t",
        "SELECT 1 ; DELETE FROM t",
        "select 1;COPY t TO '/tmp/exfil.csv'",
        "WITH x AS (SELECT 1) SELECT * FROM x; ATTACH 'other.db'",
        "DESCRIBE SELECT 1; CREATE TABLE evil AS SELECT 1",
    ],
)
def test_multi_statement_rejected_even_when_first_statement_reads(sql):
    """The gate matches the leading keyword, so a write can ride in behind a
    read unless the submission is required to be a single statement."""
    out = run_sql(sql)
    assert out.startswith("ERROR: submit one statement per call"), out


def test_multi_statement_write_does_not_reach_the_engine(local_conn):
    """End-to-end proof, not just the gate's return value: before the fix the
    table was really dropped and COPY TO really wrote a file."""
    local_conn.execute("CREATE TABLE victim AS SELECT 1 AS a")

    def victim_exists():
        return local_conn.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name='victim'"
        ).fetchone()[0]

    assert victim_exists() == 1
    assert run_sql("SELECT 1; DROP TABLE victim").startswith("ERROR")
    assert victim_exists() == 1, "second statement must never reach the engine"


def test_trailing_semicolon_is_still_one_statement(local_conn):
    assert not run_sql("SELECT 1 AS n;").startswith("ERROR")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'a;b' AS x",  # semicolon inside a string literal
        "SELECT 1 AS n -- ; not a statement",  # line comment
        "SELECT 1 AS n /* ; not a statement */",  # block comment
    ],
)
def test_semicolons_that_are_not_statement_separators_are_allowed(sql, local_conn):
    """A hand-rolled scan for ';' would false-reject these; delegating the
    split to DuckDB's own parser gets them right for free."""
    assert not run_sql(sql).startswith("ERROR"), sql


# ---------------- 2. raw file access: allowlist, not denylist ----------------
# Every one of these exists in DuckDB's function catalogue and reads bytes the
# table policies do not cover. Only the first two were caught before the fix.
@pytest.mark.parametrize(
    "func",
    [
        "read_parquet",
        "read_csv",
        "parquet_scan",  # direct alias of read_parquet
        "read_csv_auto",
        "read_json",  # was listed in the old denylist but never matched
        "read_json_auto",
        "read_ndjson",
        "read_text",
        "read_blob",
        "glob",
    ],
)
def test_every_file_reading_table_function_is_rejected(func):
    governance.set_active_role(INVESTIGATOR)
    with pytest.raises(ValueError, match="raw file access"):
        governance.apply_governance(f"SELECT 1 FROM {func}('s3://aws-public-blockchain/x')")


def test_raw_path_rejection_reports_the_offending_function_name(func=None):
    """sqlglot models a couple of functions as classes and everything else as
    Anonymous; the error must name the function either way."""
    governance.set_active_role(INVESTIGATOR)
    with pytest.raises(ValueError, match="parquet_scan"):
        governance.apply_governance("SELECT 1 FROM parquet_scan('s3://b/x')")
    with pytest.raises(ValueError, match="ReadParquet"):
        governance.apply_governance("SELECT 1 FROM read_parquet('s3://b/x')")


def test_raw_path_rejection_reaches_nested_scopes():
    governance.set_active_role(INVESTIGATOR)
    for sql in (
        "SELECT (SELECT count(*) FROM read_csv_auto('s3://b/x')) AS n",
        "WITH w AS (SELECT * FROM parquet_scan('s3://b/x')) SELECT * FROM w",
        "SELECT * FROM btc_transactions JOIN glob('s3://b/*') g ON TRUE",
    ):
        with pytest.raises(ValueError, match="raw file access"):
            governance.apply_governance(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM btc_transactions",  # bare table
        "SELECT count(*) FROM s3t.blockchain.btc_transactions",  # catalog-qualified
        "SELECT count(*) FROM glue.blockchain.btc_transactions t",  # + alias
        "SELECT count(*) FROM dl.blockchain.btc_transactions",
        "WITH w AS (SELECT 1 AS a) SELECT * FROM w",  # CTE name
        "SELECT * FROM (SELECT 1 AS a) s",  # subquery
        "SELECT * FROM ws",  # session temp table
        "SELECT unnest([1, 2]) AS x",  # nested-column analysis
        "SELECT * FROM range(5)",  # pure generator, allowlisted
        "SELECT * FROM generate_series(1, 5)",
    ],
)
def test_allowlist_does_not_break_legitimate_table_references(sql):
    """Inverting the posture must not cost the queries analysts actually run —
    including unnest for BTC's nested output column."""
    governance.set_active_role(INVESTIGATOR)
    governance.apply_governance(sql)  # must not raise


def test_unrestricted_role_may_still_use_raw_paths():
    governance.set_active_role("ops:admin")
    sql = "SELECT count(*) FROM read_parquet('s3://aws-public-blockchain/x')"
    assert governance.apply_governance(sql) == sql


# ---------------- 3. unknown role must fail CLOSED --------------------------
def _two_policy_tenant(monkeypatch):
    """A tenant with TWO policies, admin declared FIRST.

    This shape is the point: with one policy per tenant (the built-in table)
    "return the first matching key" and "return the most restricted" are
    indistinguishable, which is why the order-dependent bug passed every test.
    """
    monkeypatch.setattr(
        governance,
        "POLICIES",
        {
            "ops:admin": {"allow_raw_paths": True, "tables": {}},
            "cex-a:admin": {"allow_raw_paths": True, "tables": {}},
            "cex-a:analyst": {
                "allow_raw_paths": False,
                "tables": {
                    "btc_transactions": {
                        "row_filter": "date >= '2026-08-24'",
                        "deny_columns": ["fee"],
                    }
                },
            },
        },
    )
    monkeypatch.setattr(governance, "DEFAULT_ROLE", "ops:admin")


def test_unknown_role_falls_back_to_most_restricted_not_first_declared(monkeypatch):
    _two_policy_tenant(monkeypatch)
    assert governance.resolve_role({"tenant": "cex-a", "role": "intern"}) == "cex-a:analyst"


def test_unknown_role_fallback_is_independent_of_declaration_order(monkeypatch):
    _two_policy_tenant(monkeypatch)
    reordered = {
        "cex-a:analyst": governance.POLICIES["cex-a:analyst"],
        "cex-a:admin": governance.POLICIES["cex-a:admin"],
        "ops:admin": governance.POLICIES["ops:admin"],
    }
    monkeypatch.setattr(governance, "POLICIES", reordered)
    assert governance.resolve_role({"tenant": "cex-a", "role": "intern"}) == "cex-a:analyst"


def test_unknown_role_fallback_actually_restricts_at_the_engine(monkeypatch):
    """The fallback role must really rewrite, not merely be named."""
    _two_policy_tenant(monkeypatch)
    governance.set_active_role(governance.resolve_role({"tenant": "cex-a", "role": "intern"}))
    out = governance.apply_governance("SELECT count(*) FROM btc_transactions")
    assert "date >= '2026-08-24'" in out and "EXCLUDE (fee)" in out
    with pytest.raises(ValueError, match="raw file access"):
        governance.apply_governance("SELECT 1 FROM read_parquet('s3://b/x')")


def test_exact_role_match_still_wins_over_fallback(monkeypatch):
    _two_policy_tenant(monkeypatch)
    assert governance.resolve_role({"tenant": "cex-a", "role": "admin"}) == "cex-a:admin"


def test_restrictiveness_ranks_raw_path_denial_above_table_count(monkeypatch):
    """Denying raw file access outranks table count: a path read bypasses the
    table policies entirely, so it is the widest hole available."""
    monkeypatch.setattr(
        governance,
        "POLICIES",
        {
            "t:wide": {"allow_raw_paths": True, "tables": {"a": {}, "b": {}, "c": {}}},
            "t:narrow": {"allow_raw_paths": False, "tables": {}},
        },
    )
    assert governance.resolve_role({"tenant": "t", "role": "zzz"}) == "t:narrow"


# ---------------------------------------- SQL-injection surface of the rewrite
# These pin the reasoning behind the two `# nosec B608` annotations. Without
# them the justification lives only in a comment, and comments rot.
def test_schema_probe_reference_cannot_carry_a_second_statement():
    """`ref` reaches a bare c.execute() in agent._probe, bypassing both the
    gate and governance, so a payload escaping the identifier would be a full
    write-gate bypass. sqlglot keeps identifiers quoted, which prevents it."""
    import duckdb
    from sqlglot import exp, parse_one

    for hostile in (
        'SELECT 1 FROM "a; DROP TABLE victim".btc_transactions',
        'SELECT 1 FROM cat."s; DROP TABLE victim".btc_transactions',
        'SELECT 1 FROM "a"" ; DROP TABLE victim".btc_transactions',
    ):
        tbl = next(
            t
            for t in parse_one(hostile, read="duckdb").find_all(exp.Table)
            if t.name.lower() == "btc_transactions"
        )
        ref = tbl.copy()
        ref.set("alias", None)
        probe = f"DESCRIBE SELECT * FROM {ref.sql(dialect='duckdb')} LIMIT 0"
        assert len(duckdb.extract_statements(probe)) == 1, probe


def test_hostile_alias_cannot_append_a_statement_to_the_rewrite(local_conn):
    """`alias` is a raw model-supplied string interpolated into the guard
    template. Only the exp.Subquery is extracted from the parse, so a trailing
    statement is discarded instead of emitted."""
    import duckdb

    local_conn.execute(
        "CREATE TABLE eth_traces AS "
        "SELECT '2025-02-21' AS date, '0xabc' AS from_address, 1.5 AS value"
    )
    local_conn.execute("CREATE TABLE victim AS SELECT 1 AS a")
    governance.set_active_role(INVESTIGATOR)

    hostile = 'SELECT 1 FROM eth_traces AS "x; DROP TABLE victim"'
    governed = governance.apply_governance(hostile)
    assert "DROP" not in governed.upper(), governed
    assert len(duckdb.extract_statements(governed)) == 1

    run_sql(hostile)
    still_there = local_conn.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name='victim'"
    ).fetchone()[0]
    assert still_there == 1, "rewrite must never emit an attacker's statement"


# ------------------------------------------------- streaming trace tool (P2)
def _drain(agen):
    """Collect every item an async generator yields, synchronously."""
    import asyncio

    async def _collect():
        return [item async for item in agen]

    return asyncio.run(_collect())


def test_streaming_tool_emits_gate_rewrite_execute_then_result(local_conn):
    """The async run_sql yields the live trace phases, and its LAST yield is
    the same result string the sync core returns (what the model reads)."""
    events = _drain(agent_mod.run_sql._tool_func("SELECT id FROM t WHERE id < 3"))
    phases = [e["phase"] for e in events if isinstance(e, dict)]
    assert phases == ["gate", "rewrite", "execute"]
    gate = next(e for e in events if e.get("phase") == "gate")
    assert gate["allowed"] is True
    execute = next(e for e in events if e.get("phase") == "execute")
    assert execute["engine"] == "in-process duckdb"
    assert execute["rows"] == 3 and execute["engine_ms"] >= 0
    # final yield is the ToolResult string in the same format as the sync core
    # (engine_ms varies run-to-run, so compare the stable rows + metadata parts)
    final = events[-1]
    assert json.loads(final.split("\n")[0]) == json.loads(
        run_sql("SELECT id FROM t WHERE id < 3").split("\n")[0]
    )
    assert '"rows": 3' in final and '"governed": false' in final


def test_streaming_tool_gate_rejection_short_circuits(local_conn):
    events = _drain(agent_mod.run_sql._tool_func("DROP TABLE t"))
    assert events[0]["phase"] == "gate" and events[0]["allowed"] is False
    assert events[-1].startswith("ERROR: only")


def test_streaming_tool_reports_rewrite_for_restricted_principal(local_conn):
    local_conn.execute(
        "CREATE TABLE cex_deposit_labels AS "
        "SELECT 'exchange-a' AS exchange, 'c1' AS subject_identity, '0xabc' AS address"
    )
    governance.set_active_role(INVESTIGATOR)
    events = _drain(agent_mod.run_sql._tool_func("SELECT address FROM cex_deposit_labels"))
    rewrite = next(e for e in events if e.get("phase") == "rewrite")
    assert rewrite["governed"] is True
    assert rewrite["principal"] == INVESTIGATOR
    assert "exchange-a" in rewrite["rewritten_sql"]
    # the trace names WHICH row filter was applied (for the UI govern badge)
    assert any(r["table"] == "cex_deposit_labels" for r in rewrite["rls"])


def test_streaming_tool_reports_governance_rejection_for_denied_column(local_conn):
    local_conn.execute(
        "CREATE TABLE cex_deposit_labels AS "
        "SELECT 'exchange-a' AS exchange, 'c1' AS subject_identity, '0xabc' AS address"
    )
    governance.set_active_role(JUNIOR)
    events = _drain(agent_mod.run_sql._tool_func("SELECT subject_identity FROM cex_deposit_labels"))
    rewrite = next(e for e in events if e.get("phase") == "rewrite")
    assert rewrite.get("rejected") is True
    assert "subject_identity" in rewrite["reason"]
    assert events[-1].startswith("ERROR:")  # clean denial, not data


# --------------------------------------------- entrypoint event envelope (P2)
def test_map_event_translates_strands_events_and_accumulates_trace():
    trace = []
    assert agent_mod._map_event({"reasoningText": "thinking"}, trace)["kind"] == "reasoning"
    txt = agent_mod._map_event({"data": "answer chunk"}, trace)
    assert txt == {"kind": "text", "text": "answer chunk"}
    tool = agent_mod._map_event(
        {"current_tool_use": {"toolUseId": "t1", "name": "run_sql", "input": "SELECT 1"}}, trace
    )
    assert tool["kind"] == "tool_start" and tool["tool"] == "run_sql"
    # a run_sql phase yield surfaces as a trace envelope AND is accumulated
    ph = agent_mod._map_event(
        {"tool_stream_event": {"tool_use": {}, "data": {"phase": "execute", "engine_ms": 5}}}, trace
    )
    assert ph["kind"] == "trace" and ph["phase"] == "execute"
    assert trace == [{"phase": "execute", "engine_ms": 5}]
    # unmapped events are dropped
    assert agent_mod._map_event({"init_event_loop": True}, trace) is None


def test_map_event_does_not_confuse_tool_stream_data_with_text():
    """A tool_stream_event dict must not be mis-read as an assistant text delta."""
    trace = []
    ev = {"tool_stream_event": {"tool_use": {}, "data": {"phase": "gate", "allowed": True}}}
    out = agent_mod._map_event(ev, trace)
    assert out["kind"] == "trace"


# ═══════════════════════════════════════════════════════════════════════════
# Code validation, round 2 — the authorization audit (see docs/design.md):
# local-name shadowing, opaque-statement fail-open, deny-all default,
# policy-table validation, LABELS_PREFIX validation
# ═══════════════════════════════════════════════════════════════════════════
THREE_TENANT_LABELS = (
    "CREATE TABLE cex_deposit_labels AS SELECT * FROM (VALUES "
    "('0xa1', 'exchange-a', 'hot', 'cust-A1', 'high'), "
    "('0xb1', 'exchange-b', 'hot', 'cust-B1', 'low'), "
    "('0xc1', 'exchange-c', 'hot', 'cust-C1', 'mid')) "
    "t(address, exchange, label, subject_identity, risk_tier)"
)
FOREIGN_PII = ("cust-B1", "cust-C1")


@pytest.fixture()
def labels_conn(local_conn):
    """Three tenants' deposit labels — the shape the live bypass leaked."""
    local_conn.execute(THREE_TENANT_LABELS)
    return local_conn


# ---------------- 1. a local name may not shadow a governed table ------------
@pytest.mark.parametrize(
    "sql",
    [
        # the audit's reproduction: a CTE named like the governed table made BOTH
        # references "local" — inner base-table ref unguarded, CLS check skipped
        "WITH cex_deposit_labels AS (SELECT * FROM cex_deposit_labels) "
        "SELECT subject_identity FROM cex_deposit_labels",
        # identifiers are case-insensitive
        "WITH CEX_DEPOSIT_LABELS AS (SELECT * FROM cex_deposit_labels) "
        "SELECT * FROM cex_deposit_labels",
        # materialised into a temp table for later plain reads
        "CREATE TEMP TABLE stolen AS WITH cex_deposit_labels AS "
        "(SELECT * FROM cex_deposit_labels) SELECT * FROM cex_deposit_labels",
        # CTAS target named like the governed table exempted the right-hand source
        "CREATE TEMP TABLE cex_deposit_labels AS SELECT * FROM cex_deposit_labels",
        "CREATE TEMP VIEW cex_deposit_labels AS SELECT * FROM cex_deposit_labels",
        # nested scope and recursive-CTE variants
        "SELECT * FROM (WITH cex_deposit_labels AS (SELECT * FROM cex_deposit_labels) "
        "SELECT * FROM cex_deposit_labels) x",
        "WITH RECURSIVE cex_deposit_labels AS (SELECT * FROM cex_deposit_labels WHERE 1 = 0 "
        "UNION ALL SELECT * FROM cex_deposit_labels) SELECT * FROM cex_deposit_labels",
        # derived-table and table aliases
        "SELECT * FROM (SELECT 1) AS cex_deposit_labels",
        "SELECT * FROM eth_traces AS cex_deposit_labels",
    ],
)
def test_local_name_shadowing_a_governed_table_is_rejected(sql):
    governance.set_active_role(JUNIOR)
    with pytest.raises(ValueError, match="shadow governed table"):
        governance.apply_governance(sql)


def test_cte_shadow_never_reaches_the_engine_end_to_end(labels_conn):
    """The audit's live bypass replayed offline: before the fix this returned every
    tenant's subject_identity to the junior principal (3 rows of KYC PII)."""
    governance.set_active_role(JUNIOR)
    out = run_sql(
        "WITH cex_deposit_labels AS (SELECT * FROM cex_deposit_labels) "
        "SELECT subject_identity FROM cex_deposit_labels"
    )
    assert out.startswith("ERROR: governance:")
    for pii in ("cust-A1", *FOREIGN_PII):
        assert pii not in out
    # ...and the temp-table variant leaves nothing behind to read later
    assert run_sql(
        "CREATE TEMP TABLE stolen AS WITH cex_deposit_labels AS "
        "(SELECT * FROM cex_deposit_labels) SELECT * FROM cex_deposit_labels"
    ).startswith("ERROR: governance:")
    assert "stolen" in run_sql("SELECT * FROM stolen")  # engine: no such table


def test_temp_table_materialisation_carries_only_governed_rows(labels_conn):
    """A legitimately named working set built from a governed table holds ONLY
    the principal's rows and columns, so later ungoverned reads of the temp
    table cannot widen access."""
    governance.set_active_role(JUNIOR)
    created = run_sql("CREATE TEMP TABLE ws AS SELECT * FROM cex_deposit_labels")
    assert not created.startswith("ERROR"), created
    rows = json.loads(run_sql("SELECT * FROM ws").split("\n")[0])
    assert [r["exchange"] for r in rows] == ["exchange-a"]
    assert "subject_identity" not in rows[0]


def test_legitimate_cte_over_a_governed_table_is_still_governed(labels_conn):
    governance.set_active_role(JUNIOR)
    sql = "WITH w AS (SELECT * FROM cex_deposit_labels) SELECT * FROM w"
    governed = governance.apply_governance(sql)
    assert governed.count("exchange = 'exchange-a'") == 1
    assert "EXCLUDE (subject_identity)" in governed
    rows = json.loads(run_sql(sql).split("\n")[0])
    assert len(rows) == 1 and "subject_identity" not in rows[0]


def test_self_alias_of_a_governed_table_is_not_a_shadow():
    governance.set_active_role(JUNIOR)
    out = governance.apply_governance(
        "SELECT address FROM cex_deposit_labels AS cex_deposit_labels"
    )
    assert "exchange = 'exchange-a'" in out


# ---------------- 2. statement shapes the rewrite cannot see through --------
@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN SELECT subject_identity FROM cex_deposit_labels",
        "EXPLAIN ANALYZE SELECT count(*) FROM cex_deposit_labels",
        "SHOW cex_deposit_labels",  # sqlglot: opaque Command, no table nodes
    ],
)
def test_opaque_statements_are_rejected_for_restricted_principals(sql):
    """sqlglot parses EXPLAIN into an opaque Command with NO exp.Table nodes, so
    the rewrite loop found nothing to guard and passed the statement through;
    EXPLAIN ANALYZE then executed the ungoverned query and reported real
    cardinalities of denied rows in the plan."""
    governance.set_active_role(JUNIOR)
    with pytest.raises(ValueError, match="cannot be governed"):
        governance.apply_governance(sql)


def test_explain_analyze_does_not_execute_for_restricted_principal(labels_conn):
    governance.set_active_role(JUNIOR)
    out = run_sql("EXPLAIN ANALYZE SELECT count(*) FROM cex_deposit_labels")
    assert out.startswith("ERROR: governance:")
    assert "Query Profiling" not in out and "analyzed_plan" not in out


def test_explain_still_available_to_the_unrestricted_role():
    sql = "EXPLAIN SELECT 1"
    assert governance.apply_governance(sql) == sql


def test_describe_and_summarize_of_a_governed_table_are_governed(labels_conn):
    """DESCRIBE <table> / SUMMARIZE <table> take a relation, so the guard is
    emitted as `DESCRIBE SELECT * FROM (guard)` — valid DuckDB, denied column
    absent. (Before: an invalid `DESCRIBE (...) AS x` — closed by accident.)"""
    governance.set_active_role(JUNIOR)
    for stmt in ("DESCRIBE cex_deposit_labels", "SUMMARIZE cex_deposit_labels"):
        out = run_sql(stmt)
        assert not out.startswith("ERROR"), out
        assert "subject_identity" not in out
        assert '"governed": true' in out


def test_pivot_operates_on_governed_rows_not_the_reverse(labels_conn):
    """The PIVOT modifier is moved off the table node onto the guard, so the
    policy applies to the base relation first and the pivot sees only
    permitted rows."""
    governance.set_active_role(JUNIOR)
    sql = "SELECT * FROM cex_deposit_labels PIVOT (count(*) FOR label IN ('hot'))"
    governed = governance.apply_governance(sql)
    assert governed.index("exchange = 'exchange-a'") < governed.index("PIVOT")
    rows = json.loads(run_sql(sql).split("\n")[0])
    assert len(rows) == 1 and rows[0]["exchange"] == "exchange-a"


# ---------------- 3. missing identity must land on the deny-all floor -------
def test_default_role_is_deny_all():
    assert governance.DEFAULT_ROLE == governance.DENY_ALL_ROLE
    assert governance.POLICIES[governance.DENY_ALL_ROLE]["deny_all"] is True


@pytest.mark.parametrize(
    "claims",
    [
        None,
        {},
        {"tenant": "", "role": ""},
        {"tenant": "nobody", "role": "admin"},
        {"role": "admin"},
    ],
)
def test_unusable_claims_resolve_to_deny_all(claims):
    """Blank or unknown-tenant claims used to resolve to ops:admin."""
    assert governance.resolve_role(claims) == governance.DENY_ALL_ROLE


def test_deny_all_rejects_every_statement_before_parsing(local_conn):
    governance.set_active_role(governance.DENY_ALL_ROLE)
    assert governance.is_denied_all()
    for sql in ("SELECT 1", "SELECT * FROM t", "DESCRIBE t", "SELECT ??? FROM !!!"):
        with pytest.raises(ValueError, match="access denied"):
            governance.apply_governance(sql)
    assert run_sql("SELECT 1").startswith("ERROR: governance:")


def test_unknown_role_key_falls_to_deny_all_not_admin():
    governance.set_active_role("no-such:role")
    assert governance.active_role() == governance.DENY_ALL_ROLE


def test_deny_all_outranks_every_other_policy_in_restrictiveness(monkeypatch):
    monkeypatch.setattr(
        governance,
        "POLICIES",
        {
            "t:none": {"allow_raw_paths": False, "deny_all": True, "tables": {}},
            "t:analyst": {"allow_raw_paths": False, "tables": {"a": {"row_filter": "x = 1"}}},
            governance.DENY_ALL_ROLE: {"allow_raw_paths": False, "deny_all": True, "tables": {}},
        },
    )
    assert governance.resolve_role({"tenant": "t", "role": "zzz"}) == "t:none"


def test_entrypoint_short_circuits_deny_all_without_running_the_agent(monkeypatch):
    """No identity → one clear refusal envelope; the model is never invoked."""
    called = []
    monkeypatch.setattr(agent_mod.agent, "stream_async", lambda *_a, **_k: called.append(1))
    events = _drain(agent_mod.invoke({"prompt": "hi"}, context=None))
    assert not called
    assert [e["kind"] for e in events] == ["start", "text", "result"]
    assert events[0]["principal"] == governance.DENY_ALL_ROLE
    assert "Access denied" in events[1]["text"]
    assert events[-1]["denied"] is True


# ---------------- 4. the policy table is validated at load (T011 / M013) ----
@pytest.mark.parametrize(
    "bad",
    [
        {"ops-admin": {"allow_raw_paths": True, "tables": {}}},  # not <tenant>:<role>
        # raw paths bypass table rules — the combination promises what it can't keep
        {"t:r": {"allow_raw_paths": True, "tables": {"a": {"row_filter": "x = 1"}}}},
        {"t:r": {"tables": {"a": {"row_filter": "x = 1; DROP TABLE a"}}}},  # 2 statements
        {"t:r": {"tables": {"a": {"row_filter": "x = 1) OR (1 = 1"}}}},  # unbalanced
        {"t:r": {"tables": {"a": {"deny_columns": ["c) FROM t --"]}}}},  # not an identifier
        {"t:r": {"tables": {"a b": {}}}},  # table name not an identifier
        {"t:r": {"tables": {"a": {"rowfilter": "x = 1"}}}},  # typo'd rule key
        {"t:r": {"allow_raw_paths": "yes", "tables": {}}},  # flag not boolean
        {"t:r": {"tables": {}, "extra": 1}},  # unknown policy key
        # the floor itself cannot be redefined as something permissive
        {"t:r": {"tables": {}}, "deny-all": {"allow_raw_paths": True, "tables": {}}},
    ],
)
def test_malformed_policy_tables_are_rejected_at_load(bad):
    bad.setdefault(governance.DENY_ALL_ROLE, {"deny_all": True, "tables": {}})
    with pytest.raises(ValueError, match="governance:"):
        governance._validate_policies(bad)


def test_shipped_policy_table_validates_and_contains_the_floor():
    governance._validate_policies(governance.POLICIES)
    assert governance.DENY_ALL_ROLE in governance.POLICIES


# ---------------- 5. LABELS_PREFIX is validated, never interpolated blindly --
@pytest.mark.parametrize(
    "prefix",
    [
        "s3://attacker-bucket/labels",  # not the solution bucket, no allowlist
        # the string is interpolated into read_parquet('<prefix>/...'): closing
        # quote + UNION must be impossible
        "s3://duckdb-agent-ducklake-123456789012/l') UNION ALL SELECT * FROM read_parquet('s3://x/y",
        "s3://Duckdb-Agent-Ducklake-123456789012/labels",  # uppercase bucket
        "gs://duckdb-agent-ducklake-123456789012/labels",  # other scheme
        "/tmp/labels",  # local path
        "s3://duckdb-agent-ducklake-123456789012/la bels",  # whitespace
        "s3://duckdb-agent-ducklake-123456789012/../other",
    ],
)
def test_label_prefix_outside_the_allowlist_or_malformed_is_rejected(prefix):
    with pytest.raises(ValueError, match="LABELS_PREFIX"):
        validate_labels_prefix(prefix)


def test_label_prefix_accepts_solution_bucket_and_explicit_allowlist():
    ok = validate_labels_prefix("s3://duckdb-agent-ducklake-123456789012/labels/")
    assert ok == "s3://duckdb-agent-ducklake-123456789012/labels"
    assert (
        validate_labels_prefix("s3://my-labels/v2", frozenset({"my-labels"})) == "s3://my-labels/v2"
    )
    with pytest.raises(ValueError, match="LABELS_ALLOWED_BUCKETS"):
        validate_labels_prefix("s3://other/v2", frozenset({"my-labels"}))
