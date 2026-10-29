"""Per-statement audit line + CloudWatch metrics.

One structured JSON line per `run_sql` call, emitted on stdout in CloudWatch
**Embedded Metric Format** (EMF): the runtime's log group keeps the full
record (principal, SQL, what governance and the cost policy did, shape, rows,
engine time, materialized ref, rejection reason) and CloudWatch extracts the
numeric fields as metrics without any PutMetricData call or IAM permission.

This is an audit *log*, not an audit store (a deliberate non-goal): retention, search
and alerting are CloudWatch's job. The same record feeds the session counters
that the final `result` envelope carries to the UI's Trace tab.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

NAMESPACE = os.environ.get("AUDIT_METRIC_NAMESPACE", "DataAgentOnDuckDB")
ENABLED = os.environ.get("AUDIT_LOG", "1") == "1"
SQL_MAX = 2000
# The agent's own identity, next to the user's. AgentCore does not expose
# the runtime's workload identity to the container as an environment variable
# (checked on a deployed runtime: neither BEDROCK_AGENTCORE_WORKLOAD_IDENTITY_NAME nor a
# runtime-id variable is present), so the operator labels the deployment —
# `--env AGENT_IDENTITY=duckdb_analyst` / `duckdb_analyst_mcp`. Falls back to the
# surface name, then "local". A non-secret service label either way.
AGENT_IDENTITY = (
    os.environ.get("AGENT_IDENTITY")
    or os.environ.get("BEDROCK_AGENTCORE_WORKLOAD_IDENTITY_NAME")
    or os.environ.get("AGENT_SURFACE")
    or "local"
)

_session = {
    "statements": 0,
    "rejected": 0,
    "materialized": 0,
    "engine_ms": 0.0,
    "rows": 0,
    "expanded_paths": 0,
    "input_tokens": 0,
}


class _LineTracker:
    """stdout wrapper that remembers whether the last write ended a line.

    Strands' printing handler streams the model's answer to stdout WITHOUT a
    trailing newline, so an EMF line written right after it was glued to prose
    and CloudWatch could not extract its metrics (seen in the cloud twice, the
    second time for tool-time refusals). Knowing the column lets `record`
    start a fresh line only when it has to."""

    def __init__(self, stream) -> None:
        self._stream = stream
        # Position unknown until we have written something ourselves: anything
        # already on the line (streamed prose) happened before we wrapped, so
        # the first record after wrapping starts a fresh line defensively.
        self.at_line_start = False

    def write(self, s):
        if s:
            self.at_line_start = s.endswith("\n")
        return self._stream.write(s)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _tracked_stdout() -> _LineTracker:
    if not isinstance(sys.stdout, _LineTracker):
        sys.stdout = _LineTracker(sys.stdout)
    return sys.stdout


# The turns this session answered, newest last: the question, and the
# provenance tier of every statement that EXECUTED for it. Online feedback
# names a turn; its bucket is read from here. Bounded (a session can be long).
_TURNS_KEPT = 20
_turns: dict[str, dict] = {}


def begin_turn(question: str) -> str:
    """Open a new turn; statements recorded from now on belong to it."""
    import uuid

    tid = uuid.uuid4().hex[:12]
    _turns[tid] = {"question": (question or "")[:SQL_MAX], "provenance": []}
    while len(_turns) > _TURNS_KEPT:
        _turns.pop(next(iter(_turns)))
    return tid


def turn(turn_id: str | None = None) -> tuple[str | None, dict | None]:
    """A turn by id, or the latest one; (None, None) when there is none."""
    if turn_id:
        return (turn_id, _turns[turn_id]) if turn_id in _turns else (None, None)
    if not _turns:
        return None, None
    tid = next(reversed(_turns))
    return tid, _turns[tid]


def reset_session() -> None:
    _turns.clear()
    _session.update(
        statements=0,
        rejected=0,
        materialized=0,
        engine_ms=0.0,
        rows=0,
        expanded_paths=0,
        input_tokens=0,
    )


def note_tokens(input_tokens: int) -> None:
    """Record the turn's measured input tokens: the audit line and the
    session budget both need the number the model actually paid for."""
    _session["input_tokens"] = max(_session["input_tokens"], int(input_tokens or 0))


def session_counters(compressed: int = 0) -> dict:
    return {**_session, "engine_ms": round(_session["engine_ms"], 1), "compressed": compressed}


def record(
    *,
    principal: str,
    sql: str,
    outcome: str,  # executed | rejected | error
    stage: str
    | None = None,  # gate|handle|semantic|governance|cost|budget|upgrade|engine|session|source
    reason: str | None = None,
    governed: bool = False,
    rls: list | None = None,
    cls: list | None = None,
    cost: dict | None = None,
    shape: str | None = None,
    rows: int | None = None,
    engine_ms: float | None = None,
    materialized: str | None = None,
    tool: str = "run_sql",
    metric: str | None = None,
    owner: str | None = None,
    user_sub: str | None = None,
    source: str | None = None,
    degraded: str | None = None,
    bytes_scanned: int | None = None,
    s3_requests: int | None = None,
    provenance: str | None = None,
) -> dict:
    """Build, emit and count one audit record. Never raises."""
    rec = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [["Outcome"]],
                    "Metrics": [
                        {"Name": "Statements", "Unit": "Count"},
                        {"Name": "EngineMs", "Unit": "Milliseconds"},
                        {"Name": "Rows", "Unit": "Count"},
                        {"Name": "Materialized", "Unit": "Count"},
                        {"Name": "ExpandedPaths", "Unit": "Count"},
                        {"Name": "BytesScanned", "Unit": "Bytes"},
                        {"Name": "S3Requests", "Unit": "Count"},
                    ],
                }
            ],
        },
        "Outcome": outcome,
        "Statements": 1,
        "EngineMs": float(engine_ms or 0.0),
        "Rows": int(rows or 0),
        "Materialized": 1 if materialized else 0,
        "ExpandedPaths": int((cost or {}).get("expanded_paths") or 0),
        "BytesScanned": int(bytes_scanned or 0),
        "S3Requests": int(s3_requests or 0),
        "audit": {
            "tool": tool,
            "principal": principal,
            "sql_sha256": hashlib.sha256(sql.encode()).hexdigest()[:16],
            "sql": sql[:SQL_MAX],
            "stage": stage,
            "reason": (reason or "")[:500] or None,
            "governed": governed,
            "rls": rls or [],
            "cls": cls or [],
            "cost": cost or None,
            "shape": shape,
            "rows": int(rows or 0),  # also a metric; here so one Logs Insights row is complete
            "materialized": materialized,
            "metric": metric,  # "<name>@<version>" when the statement came from call_metric
            "owner": owner,  # session owner digest the `_r_<n>` handles are bound to
            # The two identity chains, joinable on user_sub + session:
            "agent_identity": AGENT_IDENTITY,  # WHICH agent
            "user_sub": user_sub,  # ON WHOSE BEHALF (digest, never the raw sub)
            "input_tokens": _session["input_tokens"],  # what the turn has cost so far
            # Rows that did not come from the lake:
            "source": source,  # the gateway tool that produced them
            "degraded": degraded,  # "2lo" when the source saw a service identity, not the user
            # What the statement cost at the storage layer (None when the
            # engine build lacks the instrumentation, 0 for a local-table read):
            "bytes_scanned": bytes_scanned,
            "s3_requests": s3_requests,
            # Where the statement's numbers come from (metric | verified |
            # free_sql; None for non-statement tools). The online eval buckets
            # accuracy by this marker.
            "provenance": provenance,
        },
    }
    if provenance and outcome == "executed" and _turns:
        _turns[next(reversed(_turns))]["provenance"].append(provenance)
    _session["statements"] += 1
    if outcome != "executed":
        _session["rejected"] += 1
    if materialized:
        _session["materialized"] += 1
    _session["engine_ms"] += float(engine_ms or 0.0)
    _session["rows"] += int(rows or 0)
    _session["expanded_paths"] += int((cost or {}).get("expanded_paths") or 0)
    emit(rec)
    return rec


def emit(rec: dict) -> None:
    """Write one EMF line. Never raises."""
    if ENABLED:
        try:
            out = _tracked_stdout()
            if not out.at_line_start:
                out.write("\n")  # never glue the EMF JSON to streamed prose
            out.write(json.dumps(rec, default=str) + "\n")
            out.flush()
        except Exception:  # noqa: BLE001, S110 — auditing must never break the tool  # nosec B110
            pass
