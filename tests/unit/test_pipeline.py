"""The statement pipeline: ordered stages, one audit per statement, and the
extension point a new layer plugs into."""

import asyncio

import agent as agent_mod
from data_agent import audit, governance, pipeline


def _phases(items):
    return [i["phase"] for i in items if isinstance(i, dict)]


def test_stages_are_ordered_gate_govern_cost_execute():
    assert [type(s).__name__ for s in pipeline.STAGES] == ["Gate", "Govern", "Cost", "Execute"]
    assert [s.phase for s in pipeline.STAGES] == ["gate", "rewrite", "cost", "execute"]


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
