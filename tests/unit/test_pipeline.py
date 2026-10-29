"""The statement pipeline: ordered stages, one audit per statement, and the
extension point a new layer plugs into."""

import asyncio

import agent as agent_mod
from data_agent import audit, governance, pipeline
from data_agent.governance import policies


def _phases(items):
    return [i["phase"] for i in items if isinstance(i, dict)]


def test_stages_are_ordered_gate_govern_cost_execute():
    assert [type(s).__name__ for s in pipeline.STAGES] == [
        "Gate",
        "Semantic",
        "Govern",
        "Cost",
        "Execute",
    ]
    assert [s.phase for s in pipeline.STAGES] == ["gate", "semantic", "rewrite", "cost", "execute"]


def test_run_yields_trace_events_then_exactly_one_result_string(local_conn):
    items = list(pipeline.run("SELECT id FROM t WHERE id < 3"))
    assert _phases(items) == ["gate", "rewrite", "execute"]  # no partitioned source → no cost event
    assert isinstance(items[-1], str) and items[-1].startswith("[")
    assert sum(isinstance(i, str) for i in items) == 1
    # same rows line either way (the metrics line carries a wall-clock engine_ms)
    assert (
        pipeline.run_text("SELECT id FROM t WHERE id < 3").split("\n")[0]
        == items[-1].split("\n")[0]
    )


def test_a_rejection_stops_the_pipeline_and_is_audited_once(local_conn):
    audit.reset_session()
    items = list(pipeline.run("DROP TABLE t"))
    assert _phases(items) == ["gate"] and items[0]["allowed"] is False
    assert items[-1].startswith("ERROR: only SELECT")
    assert audit.session_counters()["statements"] == 1
    assert audit.session_counters()["rejected"] == 1
    governance.set_active_role("exchange-a:junior")
    items = list(pipeline.run("SELECT subject_identity FROM cex_deposit_labels"))
    assert _phases(items) == ["gate", "rewrite"] and items[1]["rejected"] is True
    assert audit.session_counters()["statements"] == 2


def test_engine_errors_are_audited_as_errors_and_returned_verbatim(local_conn):
    audit.reset_session()
    items = list(pipeline.run("SELECT nope FROM t"))
    assert _phases(items) == ["gate", "rewrite", "execute"]
    assert "error" in items[2] and items[-1].startswith("ERROR: ")
    assert audit.session_counters() == {
        "statements": 1,
        "rejected": 1,
        "materialized": 0,
        "engine_ms": 0.0,
        "rows": 0,
        "expanded_paths": 0,
        "input_tokens": 0,
        "compressed": 0,
    }


def test_a_new_stage_plugs_in_without_touching_the_tool(local_conn, monkeypatch):
    """The extension point: a semantic layer would be one more stage here."""

    class Semantic:
        phase = "semantic"

        def run(self, st):
            st.sql = st.sql.replace("FROM rows_of_t", "FROM t")  # business name → table
            yield {"phase": "semantic", "resolved": ["rows_of_t → t"]}

    stages = list(pipeline.STAGES)
    stages.insert(1, Semantic())  # after the gate, before governance
    monkeypatch.setattr(pipeline, "STAGES", stages)

    async def _collect():
        return [
            e async for e in agent_mod.run_sql._tool_func("SELECT count(*) AS n FROM rows_of_t")
        ]

    events = asyncio.run(_collect())
    assert _phases(events) == ["gate", "semantic", "rewrite", "execute"]
    assert events[-1].startswith('[{"n": "500"}]')


def test_statement_records_what_each_stage_decided(local_conn):
    st = pipeline.Statement(sql="SELECT 1", principal="ops:admin")
    for stage in pipeline.STAGES:
        list(stage.run(st))
    assert st.query_shaped and st.governed is False and st.governed_sql == "SELECT 1"
    assert st.final_sql == "SELECT 1" and st.cost == {
        "sources": [],
        "partition_filter": None,
        "expanded_paths": 0,
    }
    assert st.result["shape"] == "rows" and st.result["total_rows"] == 1


def test_provenance_marks_free_sql_and_metric_statements(local_conn, capsys):
    """The execute trace event and the audit line say where the numbers
    came from — a model-written statement is free_sql, a compiled metric is
    metric; the online eval buckets accuracy by this marker."""
    import json

    audit.reset_session()
    items = list(pipeline.run("SELECT count(*) AS n FROM t"))
    ex = next(i for i in items if isinstance(i, dict) and i.get("phase") == "execute")
    assert ex["provenance"] == "free_sql"
    meta = {"name": "m", "version": 1, "owner": "o", "table": "t"}
    items = list(pipeline.run("SELECT count(*) AS n FROM t", metric=meta))
    ex = next(i for i in items if isinstance(i, dict) and i.get("phase") == "execute")
    assert ex["provenance"] == "metric"
    lines = [ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln]
    assert [json.loads(ln)["audit"]["provenance"] for ln in lines] == ["free_sql", "metric"]


def test_an_empty_result_under_a_row_filter_says_none_you_may_see(local_conn, monkeypatch):
    """Measured live 2026-10-19: a persona whose row filter excluded the day it
    asked about got 0 rows and told the user the data was missing, then offered
    another access path. The result string now carries the distinction."""
    monkeypatch.setitem(
        policies.POLICIES,
        "t:scoped",
        {"tables": {"t": {"row_filter": "id < 0"}}},
    )
    governance.set_active_role("t:scoped")
    out = list(pipeline.run("SELECT id FROM t"))[-1]
    assert out.startswith("[]")  # no rows survive the filter
    assert "nothing YOU may see" in out and " t for your role" in out
    assert "not the same as no such data" in out
    assert "every path carries the same policy" in out
    # the shape that actually occurred live: an aggregate returns ONE row
    # containing 0, not zero rows — it must carry the note too
    agg = list(pipeline.run("SELECT count(*) AS n FROM t"))[-1]
    assert agg.startswith('[{"n": "0"}]')  # ONE row whose value is 0
    assert "nothing YOU may see" in agg


def test_a_nonempty_or_ungoverned_result_carries_no_such_note(local_conn, monkeypatch):
    monkeypatch.setitem(
        policies.POLICIES,
        "t:scoped",
        {"tables": {"t": {"row_filter": "id < 3"}}},
    )
    governance.set_active_role("t:scoped")
    assert "YOU may see" not in list(pipeline.run("SELECT id FROM t"))[-1]  # rows survive
    # a non-zero aggregate under the same filter is data: no note
    assert "YOU may see" not in list(pipeline.run("SELECT count(*) AS n FROM t"))[-1]
    governance.set_active_role("ops:admin")
    # admin: zero rows, but no row filter applied → no note (nothing was hidden)
    assert "YOU may see" not in list(pipeline.run("SELECT id FROM t WHERE id < 0"))[-1]
