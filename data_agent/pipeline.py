"""One statement through the layers: gate → resolve → govern → cost → execute → shape.

The pipeline is an ordered list of stages with one interface. A stage reads
and updates the `Statement`, yields its trace event(s) — the live
`gate → semantic → rewrite → cost → execute` line the UI renders and the eval
suite asserts on — and raises `Rejected` to stop the statement. The driver
audits exactly once per statement and turns the outcome into the string the
model reads. Adding a layer is adding a stage to STAGES; neither the tool nor
the UI changes (`ui/app.py` renders an unrecognised phase by name).

Stage order is a property, not a preference: the semantic layer EXPANDS (a
metric name becomes SQL over base tables) and governance NARROWS (row filter
injected, denied columns excluded), so semantic runs first and governance sees
the expanded statement. Semantic and Cost emit no trace event for a statement
they have nothing to do with, so a plain SQL query still traces as
`gate → rewrite → execute`.

`run()` is a plain generator so the streaming tool can pump it one item at a
time off the event loop while a synchronous caller (tests, CLI) just takes
the last item. Both paths run the same code, so the model sees an identical
result string whether or not the UI streams.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

from data_agent import audit, governance, semantic
from data_agent.compute import engine, gate
from data_agent.context import cost, results

# CLS is schema-aware: governance asks the engine which columns a relation
# exposes (DESCRIBE, cached per reference). Wired here — the point where the
# governance and compute layers meet — so neither imports the other.
governance.set_schema_prober(engine.probe_columns)
# Table-level deny-by-default needs to know which temp objects the
# session created, so a principal can read its own working sets while a
# mounted-but-unlisted table stays refused. Same wiring point, same reason.
governance.set_session_lister(engine.session_objects)


@dataclass
class Statement:
    """What the stages know about one statement, accumulated in order."""

    sql: str  # as submitted by the model, then as the semantic layer expanded it
    principal: str
    submitted: str = ""  # the original text, kept for the audit line
    query_shaped: bool = False  # SELECT/WITH → materialize-and-shape; else direct
    resolved: list = field(default_factory=list)  # metric definitions this statement used
    governed_sql: str = ""
    governed: bool = False
    applied: dict = field(default_factory=lambda: {"rls": [], "cls": []})
    final_sql: str = ""
    cost: dict | None = None
    result: dict | None = None


class Rejected(Exception):
    """A stage refused the statement (or the engine failed it).

    `reason` is what the audit line records, `text` the string the model
    reads, `audit_stage` the audit vocabulary (gate | semantic | governance |
    cost | engine), `outcome` rejected | error.
    """

    def __init__(
        self, reason: str, *, audit_stage: str, outcome: str = "rejected", text: str | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.audit_stage = audit_stage
        self.outcome = outcome
        self.text = text if text is not None else f"ERROR: {reason}"


class Stage(Protocol):
    phase: str  # the trace phase name this stage emits

    def run(self, st: Statement) -> Iterator[dict]: ...


class Gate:
    """Read-only gate (compute) + the framework's reserved result names (context)."""

    phase = "gate"

    def run(self, st: Statement) -> Iterator[dict]:
        err = gate.check(st.sql)
        if err is None:
            misuse = results.reserved_name_misuse(gate.normalize(st.sql))
            err = f"ERROR: {misuse}" if misuse else None
        if err:
            yield {"phase": "gate", "allowed": False, "sql": st.sql[:400]}
            raise Rejected(err, audit_stage="gate", text=err)
        st.query_shaped = gate.query_shaped(st.sql)
        yield {"phase": "gate", "allowed": True, "sql": st.sql[:400]}


class Semantic:
    """Semantic layer: a `metric(...)` call becomes the SQL its definition
    fixes, before any other layer sees the statement. Emits a trace event only
    for statements that call a metric, so ordinary SQL traces unchanged."""

    phase = "semantic"

    def run(self, st: Statement) -> Iterator[dict]:
        try:
            expanded, resolved = semantic.expand(st.sql)
        except semantic.MetricNotCovered as e:
            # An undefined metric, dimension or window is a refusal that names
            # what exists, so the model's next attempt is a correct call. The
            # alternative — expanding something close — answers a question
            # nobody asked with a number nobody agreed to.
            yield {"phase": "semantic", "rejected": True, "reason": str(e)}
            raise Rejected(str(e), audit_stage="semantic") from e
        if not resolved:
            return
        st.sql, st.resolved = expanded, resolved
        yield {
            "phase": "semantic",
            "metrics": [
                {
                    "metric": r["metric"],
                    "version": r["version"],
                    "unit": r["unit"],
                    "owner": r["owner"],
                    "scope": r["scope"],
                }
                for r in resolved
            ],
            "rewritten_sql": st.sql[:600],
        }


class Govern:
    """Authorization: rewrite under the caller's policy BEFORE the engine sees
    the statement (RLS predicate + column exclusion; raw-path and unparseable
    SQL rejected for restricted principals — fail closed)."""

    phase = "rewrite"

    def run(self, st: Statement) -> Iterator[dict]:
        try:
            st.governed_sql = governance.apply_governance(st.sql)
        except Exception as e:  # noqa: BLE001 — governance fails CLOSED: any error is a refusal
            # ValueError is the layer's own rejection; anything else (a sqlglot
            # ParseError from a hostile alias, T006) must still refuse and be
            # audited, never crash the turn or slip through unlogged.
            reason = str(e) if isinstance(e, ValueError) else f"rewrite failed: {e}"
            yield {"phase": "rewrite", "rejected": True, "reason": reason}
            raise Rejected(reason, audit_stage="governance") from e
        st.governed = st.governed_sql != st.sql
        st.applied = governance.describe_applied(st.sql)
        yield {
            "phase": "rewrite",
            "governed": st.governed,
            "principal": st.principal,
            "rls": st.applied["rls"],
            "cls": st.applied["cls"],
            "rewritten_sql": st.governed_sql[:600] if st.governed else None,
        }


class Cost:
    """Cost policy: required partition filter + explicit path expansion.
    Emits a trace event only for statements that touch a partitioned raw source."""

    phase = "cost"

    def run(self, st: Statement) -> Iterator[dict]:
        try:
            st.final_sql, st.cost = cost.apply(st.governed_sql)
        except cost.CostRejected as e:
            yield {"phase": "cost", "rejected": True, "reason": str(e)}
            raise Rejected(str(e), audit_stage="cost") from e
        if st.cost["sources"]:
            yield {
                "phase": "cost",
                "sources": st.cost["sources"],
                "partition_filter": st.cost["partition_filter"],
                "expanded_paths": st.cost["expanded_paths"],
                "rewritten_sql": st.final_sql[:600] if st.final_sql != st.governed_sql else None,
            }


class Execute:
    """Run under the shared-connection lock and shape the result.
    Engine errors surface verbatim so the model can fix the SQL (invariant 5)."""

    phase = "execute"

    def run(self, st: Statement) -> Iterator[dict]:
        results.STORE.touch(st.final_sql)  # reading `_r_<n>` refreshes its LRU slot
        try:
            st.result = results.execute(
                engine.get_conn(), st.final_sql, st.query_shaped, engine.conn_lock
            )
        except Exception as e:  # noqa: BLE001 — surface engine errors for self-repair
            yield {"phase": "execute", "error": str(e)}
            raise Rejected(str(e), audit_stage="engine", outcome="error") from e
        yield {
            "phase": "execute",
            "engine": "in-process duckdb",
            "engine_ms": st.result["engine_ms"],
            "rows": st.result["total_rows"],
            "shape": st.result["shape"],  # rows (inline) | summary (materialized)
            "materialized": st.result["ref"],
            "evicted": st.result["evicted"],
        }


STAGES: list[Stage] = [Gate(), Semantic(), Govern(), Cost(), Execute()]


def _audit(st: Statement, *, outcome: str, stage: str | None = None, reason: str | None = None):
    res = st.result or {}
    audit.record(
        principal=st.principal,
        # what the caller submitted, not what the semantic layer expanded it
        # into: an audit line has to show the request that was made
        sql=st.submitted or st.sql,
        semantic=[{"metric": r["metric"], "version": r["version"]} for r in st.resolved],
        outcome=outcome,
        stage=stage,
        reason=reason,
        governed=st.governed,
        rls=st.applied["rls"],
        cls=st.applied["cls"],
        cost=st.cost if st.cost and st.cost["sources"] else None,
        shape=res.get("shape"),
        rows=res.get("total_rows"),
        engine_ms=res.get("engine_ms"),
        materialized=res.get("ref"),
    )


def run(sql: str) -> Iterator[dict | str]:
    """Drive one statement through STAGES.

    Yields each stage's trace events (dicts with a "phase" key) and then
    exactly one string — the result the model reads. Every statement is
    audited once, whatever the outcome.
    """
    st = Statement(sql=sql, principal=governance.active_role(), submitted=sql)
    try:
        for stage in STAGES:
            yield from stage.run(st)
    except Rejected as r:
        _audit(st, outcome=r.outcome, stage=r.audit_stage, reason=r.reason)
        yield r.text
        return
    except Exception as e:  # noqa: BLE001 — last-resort net: every statement is audited exactly once
        # Stages convert their own failures into Rejected; this catch exists so
        # a genuinely unexpected error still produces one audit line and one
        # error string rather than an unhandled, unlogged exception (T006).
        _audit(st, outcome="error", stage="pipeline", reason=str(e))
        yield f"ERROR: {e}"
        return
    _audit(st, outcome="executed")
    text = results.format_result(st.result, st.governed)
    if st.resolved:
        # The definitions travel with the rows. The trace shows them to a human
        # reader, but the model writes the answer, so a figure quoted without
        # its scope is exactly the failure this layer exists to prevent.
        text += "\n" + semantic.source_footer(st.resolved)
    yield text


def run_text(sql: str) -> str:
    """Synchronous form: the result string only (tests, local callers)."""
    *_, last = run(sql)
    return last
