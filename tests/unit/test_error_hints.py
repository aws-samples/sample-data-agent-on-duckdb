"""Engine-error enrichment: the engine's message goes back VERBATIM
(invariant 5) with one zero-network hint appended — which table a column
actually lives on, or the nearest table name — filtered so a restricted role
never learns of a table outside its policy or a CLS-denied column name."""

import json

import pytest

from data_agent import governance, pipeline
from data_agent.compute import engine


@pytest.fixture()
def ops_conn(local_conn):
    local_conn.execute(
        "CREATE VIEW cur_line_items AS SELECT 'unset-account' AS line_item_usage_account_id, "
        "'AmazonEC2' AS line_item_product_code, 1.5 AS line_item_unblended_cost, "
        "'vol-1' AS line_item_resource_id, '2026-08' AS billing_period"
    )
    local_conn.execute(
        "CREATE VIEW cloudtrail_events AS SELECT 'unset-account' AS recipient_account_id, "
        "'AssumeRole' AS event_name, '1.2.3.4' AS source_ip_address, 'aws-cli/2' AS user_agent, "
        "'2026-09-02' AS date"
    )
    local_conn.execute("CREATE VIEW cw_logs AS SELECT 'msg' AS message, '2026-09-02' AS date")
    governance.set_schema_prober(engine.probe_columns)
    return local_conn


def _hint(items):
    ex = [i for i in items if isinstance(i, dict) and i.get("phase") == "execute"]
    return ex[0].get("hint", "") if ex else ""


def test_the_engine_error_is_verbatim_and_the_hint_names_the_right_table(ops_conn):
    items = list(
        pipeline.run("SELECT user_agent FROM cur_line_items WHERE billing_period='2026-08'")
    )
    text = items[-1]
    assert text.startswith('ERROR: Binder Error: Referenced column "user_agent" not found')
    assert "Candidate bindings" in text  # DuckDB's own message, untouched
    assert "Hint: column `user_agent` exists on: cloudtrail_events." in text
    assert _hint(items).startswith("Hint: column")


def test_a_typo_gets_the_near_miss_column_and_its_table(ops_conn):
    items = list(pipeline.run("SELECT line_item_unblendd_cost FROM cur_line_items"))
    hint = _hint(items)
    assert "closest" in hint and "line_item_unblended_cost" in hint and "cur_line_items" in hint


def test_a_mistyped_table_gets_the_nearest_table_the_role_can_query(ops_conn):
    items = list(pipeline.run("SELECT count(*) FROM cloudtrail_event"))
    text = items[-1]
    assert (
        "Catalog Error" in text
        and "Hint: closest table(s) you can query: cloudtrail_events." in text
    )


def test_the_hint_never_names_a_table_outside_the_role_policy(ops_conn):
    """secops may read cloudtrail_events only: a column that exists on
    cur_line_items / cw_logs must not be revealed by the hint."""
    governance.set_active_role("ops:secops")
    items = list(pipeline.run("SELECT message FROM cloudtrail_events WHERE date='2026-09-02'"))
    hint = _hint(items)
    assert "cw_logs" not in hint and "cur_line_items" not in hint
    assert hint == "" or "closest" in hint


def test_the_hint_never_names_a_cls_denied_column(ops_conn):
    """secops-junior loses source_ip_address / user_agent: asking for one on the
    wrong table must not surface it as "exists on cloudtrail_events"."""
    governance.set_active_role("ops:secops-junior")
    denied = governance.denied_columns()
    assert denied["cloudtrail_events"] == frozenset({"source_ip_address", "user_agent"})
    hint = engine.similar_names(
        'Binder Error: Referenced column "user_agent" not found in FROM clause!',
        governance.visible_tables(),
        denied,
    )
    assert "user_agent" not in hint.replace("`user_agent`", "") and "cloudtrail_events" not in hint


def test_no_hint_for_an_unrelated_engine_error(ops_conn):
    items = list(pipeline.run("SELECT 1/0 AS x"))
    assert _hint(items) == "" or "Hint" not in items[-1]
    assert engine.similar_names("Out of Memory Error: could not allocate") == ""


def test_hint_failure_never_breaks_the_turn(ops_conn, monkeypatch):
    monkeypatch.setattr(
        engine, "_catalog_rows", lambda: (_ for _ in ()).throw(RuntimeError("catalog gone"))
    )
    items = list(pipeline.run("SELECT nope FROM cur_line_items"))
    assert items[-1].startswith("ERROR: Binder Error") and _hint(items) == ""


def test_the_audit_line_keeps_the_engine_reason_not_the_hint(ops_conn, capsys):
    list(pipeline.run("SELECT user_agent FROM cur_line_items"))
    line = [ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln][-1]
    rec = json.loads(line)["audit"]
    assert rec["stage"] == "engine" and "Hint:" not in rec["reason"]
