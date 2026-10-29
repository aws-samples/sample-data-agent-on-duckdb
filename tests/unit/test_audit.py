"""Audit line (EMF) + session counters."""

import asyncio
import json

import agent as agent_mod
from data_agent import audit, governance


def _emf_lines(captured: str) -> list[dict]:
    out = []
    for line in captured.splitlines():
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "_aws" in d:
                out.append(d)
    return out


def test_record_emits_one_emf_line_with_metrics_and_audit_fields(capsys):
    audit.reset_session()
    rec = audit.record(
        principal="exchange-a:junior",
        sql="SELECT * FROM cex_deposit_labels",
        outcome="executed",
        governed=True,
        rls=[{"table": "cex_deposit_labels", "predicate": "exchange = 'exchange-a'"}],
        cls=[{"table": "cex_deposit_labels", "column": "subject_identity"}],
        cost={"sources": ["eth_traces"], "partition_filter": True, "expanded_paths": 3},
        shape="summary",
        rows=5213,
        engine_ms=41.5,
        materialized="_r_2",
    )
    lines = _emf_lines(capsys.readouterr().out)
    assert len(lines) == 1 and lines[0] == rec
    cw = rec["_aws"]["CloudWatchMetrics"][0]
    assert cw["Namespace"] == "DataAgentOnDuckDB" and cw["Dimensions"] == [["Outcome"]]
    assert {m["Name"] for m in cw["Metrics"]} == {
        "Statements",
        "EngineMs",
        "Rows",
        "Materialized",
        "ExpandedPaths",
        "BytesScanned",
        "S3Requests",
    }
    assert rec["Outcome"] == "executed" and rec["Statements"] == 1
    assert rec["EngineMs"] == 41.5 and rec["Rows"] == 5213
    assert rec["Materialized"] == 1 and rec["ExpandedPaths"] == 3
    a = rec["audit"]
    assert a["principal"] == "exchange-a:junior" and a["governed"] is True
    assert a["materialized"] == "_r_2" and a["shape"] == "summary"
    assert len(a["sql_sha256"]) == 16 and a["sql"].startswith("SELECT")
    assert (
        a["rls"][0]["table"] == "cex_deposit_labels" and a["cls"][0]["column"] == "subject_identity"
    )


def test_session_counters_accumulate_and_reset(capsys):
    audit.reset_session()
    audit.record(principal="p", sql="a", outcome="executed", rows=10, engine_ms=2.5)
    audit.record(principal="p", sql="b", outcome="rejected", stage="cost", reason="no date")
    audit.record(
        principal="p", sql="c", outcome="executed", rows=1, engine_ms=0.5, materialized="_r_1"
    )
    audit.note_tokens(9000)
    audit.record(principal="p", sql="d", outcome="executed", rows=0, cost={"expanded_paths": 3})
    c = audit.session_counters(compressed=4)
    assert c == {
        "statements": 4,
        "rejected": 1,
        "materialized": 1,
        "engine_ms": 3.0,
        "rows": 11,
        "expanded_paths": 3,  # the session budget's second dimension
        "input_tokens": 9000,  # measured, highest projection of the turn
        "compressed": 4,
    }
    audit.note_tokens(5000)  # a lower measurement never lowers the session total
    assert audit.session_counters()["input_tokens"] == 9000
    audit.reset_session()
    assert audit.session_counters()["statements"] == 0
    assert audit.session_counters()["input_tokens"] == 0


def test_disabled_audit_counts_but_does_not_print(capsys, monkeypatch):
    monkeypatch.setattr(audit, "ENABLED", False)
    audit.reset_session()
    audit.record(principal="p", sql="x", outcome="executed")
    assert _emf_lines(capsys.readouterr().out) == []
    assert audit.session_counters()["statements"] == 1


def test_long_sql_and_reason_are_bounded():
    rec = audit.record(principal="p", sql="S" * 5000, outcome="rejected", reason="r" * 2000)
    assert len(rec["audit"]["sql"]) == audit.SQL_MAX and len(rec["audit"]["reason"]) == 500


# ------------------------------------------------ wired into the streaming tool
def _drain(agen):
    async def _collect():
        return [item async for item in agen]

    return asyncio.run(_collect())


def test_streaming_tool_records_every_outcome(local_conn, capsys):
    audit.reset_session()
    _drain(agent_mod.run_sql._tool_func("SELECT id FROM t"))  # executed, materialized
    _drain(agent_mod.run_sql._tool_func("DROP TABLE t"))  # gate rejection
    _drain(agent_mod.run_sql._tool_func("SELECT nope FROM t"))  # engine error
    governance.set_active_role("exchange-a:junior")
    _drain(
        agent_mod.run_sql._tool_func("SELECT * FROM read_parquet('s3://x/y.parquet')")
    )  # governance
    lines = _emf_lines(capsys.readouterr().out)
    outcomes = [(d["Outcome"], d["audit"]["stage"]) for d in lines]
    assert outcomes == [
        ("executed", None),
        ("rejected", "gate"),
        ("error", "engine"),
        ("rejected", "governance"),
    ]
    assert lines[0]["Materialized"] == 1 and lines[0]["audit"]["materialized"] == "_r_1"
    assert lines[3]["audit"]["principal"] == "exchange-a:junior"
    c = audit.session_counters()
    assert c["statements"] == 4 and c["rejected"] == 3 and c["materialized"] == 1


def test_search_schema_is_audited_as_its_own_tool(local_conn, capsys):
    audit.reset_session()
    agent_mod.search_schema._tool_func("anything", 3)
    lines = _emf_lines(capsys.readouterr().out)
    assert lines and lines[-1]["audit"]["tool"] == "search_schema"
    assert lines[-1]["audit"]["sql"] == "anything"


def test_audit_line_never_glues_to_streamed_prose(capsys):
    """Strands streams the answer without a trailing newline; the next EMF
    line must still start at column 0 or CloudWatch cannot parse it (an early cloud run
    lost the call_metric refusal lines that way)."""
    import sys

    sys.stdout.write("Let me try the object range format")  # prose, no newline
    audit.record(
        principal="ops:finops", sql="call_metric('x')", outcome="rejected", stage="semantic"
    )
    audit.record(principal="ops:finops", sql="SELECT 1", outcome="executed")
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert lines[0] == "Let me try the object range format"
    assert json.loads(lines[1])["audit"]["stage"] == "semantic"
    assert json.loads(lines[2])["audit"]["sql"] == "SELECT 1"


def test_audit_line_carries_both_identity_chains_and_the_turn_cost(capsys, monkeypatch):
    """WHICH agent (agent_identity) acted ON WHOSE BEHALF (user_sub), and
    what the turn had cost when the statement ran. Both identities are
    non-secret labels; the user is a digest, never the raw sub."""
    monkeypatch.setattr(audit, "AGENT_IDENTITY", "duckdb_analyst_mcp")
    audit.reset_session()
    audit.note_tokens(12500)
    rec = audit.record(
        principal="ops:finops",
        sql="SELECT 1",
        outcome="executed",
        owner="47ee7df6e5bd",
        user_sub="47ee7df6e5bd",
    )
    a = rec["audit"]
    assert a["agent_identity"] == "duckdb_analyst_mcp" and a["user_sub"] == "47ee7df6e5bd"
    assert a["input_tokens"] == 12500 and a["owner"] == a["user_sub"]
    line = json.loads([ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln][-1])
    assert line["audit"]["agent_identity"] == "duckdb_analyst_mcp"
    # no PII: the record holds digests and service labels only
    blob = json.dumps(line)
    assert "sub:" not in blob and "@" not in blob.replace('"metric": null', "")


def test_a_pipeline_statement_fills_the_user_chain_from_the_bound_owner(local_conn, capsys):
    from data_agent import pipeline
    from data_agent.context import results

    results.STORE.bind("sub:u-42")
    pipeline.run_text("SELECT 1")
    a = json.loads([ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln][-1])[
        "audit"
    ]
    assert a["user_sub"] == results.owner_id("sub:u-42") == a["owner"]
    assert a["agent_identity"]  # always set (env, else the surface name)
