"""Cloud-ops scenario: views, cost specs, governance personas, catalog doc."""

import json

import duckdb

from data_agent import governance
from data_agent.compute import engine
from data_agent.context import cost, datamap, prompt, results
from data_agent.pipeline import run_text as run_sql
from data_agent.storage import legs, registry
from data_agent.storage.scenarios import cloudops, onchain
from data_agent.storage.scenarios.base import Scenario

PREFIX = "s3://duckdb-agent-ducklake-123456789012/ops"
CUR = "s3://cur-bucket/cur-/cur2/data"


# ------------------------------------------------------------- views/specs
def test_ops_scenario_is_off_without_env():
    assert cloudops.build_views("", "") == {}
    assert cloudops.build_sources("", "") == {}
    off = cloudops.build("", "", "", "")
    assert not off.enabled and off.doc == "" and off.macros == ()
    assert set(off.policies) == {"ops:finops", "ops:secops", "ops:secops-junior"}  # always


def test_ops_views_cover_curated_parquet_and_native_cur():
    v = cloudops.build_views(PREFIX, CUR)
    assert set(v) == {"cur_line_items", "cloudtrail_events", "cw_logs", "cur_raw"}
    assert v["cur_line_items"].startswith(f"read_parquet('{PREFIX}/cur/*/*.parquet'")
    assert "cloudtrail/*/*/*.parquet" in v["cloudtrail_events"]  # date=/region= two levels
    assert v["cur_raw"].startswith(f"read_csv('{CUR}/BILLING_PERIOD=*/*.csv.gz'")
    assert "all_varchar=true" in v["cur_raw"]


def test_ops_scenario_carries_the_cloudtrail_macro_when_the_trail_is_set():
    sc = cloudops.build(PREFIX, CUR, "s3://trail/AWSLogs/1/CloudTrail", "123456789012")
    assert sc.enabled and sc.lenient and len(sc.macros) == 1
    assert sc.macros[0].startswith("CREATE OR REPLACE MACRO cloudtrail_raw(day, region")
    assert "s3://trail/AWSLogs/1/CloudTrail/" in sc.macros[0]


def test_ops_cost_specs_expand_by_month_and_day(monkeypatch):
    specs = cloudops.build_sources(PREFIX, CUR)
    monkeypatch.setattr(cost, "SOURCES", specs)
    out, info = cost.apply(
        "SELECT sum(line_item_unblended_cost) FROM cur_line_items "
        "WHERE billing_period BETWEEN '2026-07' AND '2026-09'"
    )
    assert info["expanded_paths"] == 3 and f"{PREFIX}/cur/billing_period=2026-08/*.parquet" in out
    out, info = cost.apply(
        "SELECT count(*) FROM cloudtrail_events WHERE date IN ('2026-09-01', '2026-09-02')"
    )
    assert info["expanded_paths"] == 2 and "date=2026-09-01/*/*.parquet" in out
    out, info = cost.apply("SELECT count(*) FROM cur_raw WHERE billing_period = '2026-08'")
    assert info["expanded_paths"] == 1 and "BILLING_PERIOD=2026-08/*.csv.gz" in out
    flat = out.lower().replace(" ", "")
    assert "read_csv([" in flat and "all_varchar=true" in flat


def test_ops_sources_require_their_partition_column(monkeypatch):
    monkeypatch.setattr(cost, "SOURCES", cloudops.build_sources(PREFIX, CUR))
    for sql in (
        "SELECT sum(line_item_unblended_cost) FROM cur_line_items",
        "SELECT count(*) FROM cloudtrail_events WHERE event_name = 'AssumeRole'",
        "SELECT count(*) FROM cw_logs",
    ):
        try:
            cost.apply(sql)
        except cost.CostRejected as e:
            assert "partition column" in str(e)
        else:
            raise AssertionError(f"not rejected: {sql}")


# ------------------------------------------------------------- governance
def test_ops_personas_exist_with_enterprise_semantics():
    p = governance.POLICIES
    fin = p["ops:finops"]["tables"]
    assert set(fin) == {"cur_line_items", "cur_raw"}
    assert fin["cur_line_items"]["deny_columns"] == ["line_item_resource_id"]
    assert "line_item_usage_account_id = '" in fin["cur_line_items"]["row_filter"]
    sec = p["ops:secops"]["tables"]["cloudtrail_events"]
    assert "recipient_account_id = '" in sec["row_filter"] and not sec.get("deny_columns")
    jr = p["ops:secops-junior"]["tables"]["cloudtrail_events"]
    assert jr["deny_columns"] == ["source_ip_address", "user_agent"]
    for role in ("ops:finops", "ops:secops", "ops:secops-junior"):
        assert p[role]["allow_raw_paths"] is False


def test_unset_account_id_fails_closed_in_the_predicate():
    """Without OPS_ACCOUNT_ID the RLS predicate can never match (unit env)."""
    pred = governance.POLICIES["ops:secops"]["tables"]["cloudtrail_events"]["row_filter"]
    assert pred == "recipient_account_id = 'unset-account'"
    assert cloudops.account_id("acct-123456789012") == "123456789012"  # digits only


def test_secops_junior_rewrite_hides_ip_and_agent_and_scopes_account(local_conn):
    local_conn.execute(
        "CREATE TABLE cloudtrail_events AS SELECT * FROM (VALUES "
        "('unset-account', 'AssumeRole', '1.2.3.4', 'aws-cli', 'arn:x'), "
        "('999999999999', 'Decrypt', '5.6.7.8', 'sdk', 'arn:y')) "
        "v(recipient_account_id, event_name, source_ip_address, user_agent, user_arn)"
    )
    governance.set_active_role("ops:secops-junior")
    governed = governance.apply_governance("SELECT * FROM cloudtrail_events")
    assert "EXCLUDE (source_ip_address, user_agent)" in governed
    assert "recipient_account_id = 'unset-account'" in governed
    rows = json.loads(run_sql("SELECT * FROM cloudtrail_events").split("\n")[0])
    assert [{k: results.unmark(v) for k, v in r.items()} for r in rows] == [
        {"recipient_account_id": "unset-account", "event_name": "AssumeRole", "user_arn": "arn:x"}
    ]
    out = run_sql("SELECT source_ip_address FROM cloudtrail_events")
    assert out.startswith("ERROR: governance: column 'source_ip_address' is not visible")


def test_finops_cannot_see_resource_ids_but_secops_can_see_ips(local_conn):
    local_conn.execute(
        "CREATE TABLE cur_line_items AS SELECT 'unset-account' AS line_item_usage_account_id, "
        "'AmazonS3' AS product_servicecode, 1.5 AS line_item_unblended_cost, "
        "'arn:aws:s3:::b' AS line_item_resource_id"
    )
    governance.set_active_role("ops:finops")
    assert run_sql("SELECT line_item_resource_id FROM cur_line_items").startswith(
        "ERROR: governance:"
    )
    rows = json.loads(
        run_sql("SELECT product_servicecode, line_item_unblended_cost FROM cur_line_items").split(
            "\n"
        )[0]
    )
    assert [{k: results.unmark(v) for k, v in r.items()} for r in rows] == [
        {"product_servicecode": "AmazonS3", "line_item_unblended_cost": "1.5"}
    ]


# -------------------------------------------------------------- catalog doc
def test_catalog_doc_ops_section_is_gated():
    without = prompt.catalog_doc([], scenarios=[cloudops.build("", "", "", "")])
    with_ops = prompt.catalog_doc([], scenarios=[cloudops.build(PREFIX, CUR, "", "")])
    assert "cur_line_items" not in without
    assert "cur_line_items" in with_ops and "cloudtrail_events" in with_ops
    assert "SAME shape as a fund-flow case" in with_ops
    assert "billing_period" in with_ops and "line_item_unblended_cost (USD)" in with_ops


# ----------------------------------------------------------- engine init
def test_missing_ops_dataset_does_not_break_engine_init(monkeypatch, tmp_path):
    """A lenient scenario's view over a path with no files must be skipped and
    recorded, not kill get_conn()."""
    engine.reset()
    # no AWS credentials in CI: the credential-chain secrets are the one init
    # step that needs them, and they are irrelevant to local-file views
    monkeypatch.setattr(engine, "_s3_secrets", lambda c: None)
    monkeypatch.setattr(legs, "S3_TABLES_ARN", "")
    monkeypatch.setattr(legs, "GLUE_CATALOG", "")
    monkeypatch.setattr(legs, "DUCKLAKE_CATALOG", "")
    good = tmp_path / "cur" / "billing_period=2026-08"
    good.mkdir(parents=True)
    duckdb.connect().execute(f"COPY (SELECT 1 AS x) TO '{good}/part-0.parquet' (FORMAT parquet)")
    ops = Scenario(
        name="cloudops",
        views={
            "cur_line_items": f"read_parquet('{tmp_path}/cur/*/*.parquet', hive_partitioning=1)",
            "cw_logs": f"read_parquet('{tmp_path}/cwlogs/*/*.parquet', hive_partitioning=1)",
        },
        lenient=True,
    )
    monkeypatch.setattr(registry, "SCENARIOS", (ops,))
    try:
        conn = engine.get_conn()
        assert conn.execute("SELECT count(*) FROM cur_line_items").fetchone()[0] == 1
        assert "cw_logs" in engine.VIEW_ERRORS and "No files found" in engine.VIEW_ERRORS["cw_logs"]
        assert "cur_line_items" not in engine.VIEW_ERRORS
        conn.close()
    finally:
        engine.reset()


def test_ops_sources_carry_a_probe_partition_that_exists(monkeypatch):
    """The data map DESCRIBEs a raw source through ONE partition. The
    on-chain default (2025-02-21) matches nothing in the ops exports, so every
    ops source names its own probe partition (live reconcile finding:
    all four ops tables failed to describe and had no columns in the L1 index)."""
    monkeypatch.setattr(datamap, "PROBE_REMOTE", True)
    specs = cloudops.build_sources(PREFIX, CUR)
    assert specs["cur_line_items"]["probe"] == cloudops.OPS_PROBE_MONTH
    assert specs["cloudtrail_events"]["probe"] == cloudops.OPS_PROBE_DAY
    probe = datamap._probe_sql("cur_line_items", specs["cur_line_items"])
    assert f"billing_period={cloudops.OPS_PROBE_MONTH}/*.parquet" in probe
    assert "2025-02-21" not in probe
    # a spec without its own probe still falls back to the global knob
    probe = datamap._probe_sql("eth_traces", onchain.PARTITIONED_SOURCES["eth_traces"])
    assert f"date={datamap.PROBE_PARTITION}/*.parquet" in probe
