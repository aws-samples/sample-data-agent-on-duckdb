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

_session = {"statements": 0, "rejected": 0, "materialized": 0, "engine_ms": 0.0, "rows": 0}


def reset_session() -> None:
    _session.update(statements=0, rejected=0, materialized=0, engine_ms=0.0, rows=0)


def session_counters(compressed: int = 0) -> dict:
    return {**_session, "engine_ms": round(_session["engine_ms"], 1), "compressed": compressed}


def record(
    *,
    principal: str,
    sql: str,
    outcome: str,  # executed | rejected | error
    stage: str | None = None,  # gate | semantic | governance | cost | engine
    reason: str | None = None,
    semantic: list | None = None,  # metrics the statement resolved (name + version)
    governed: bool = False,
    rls: list | None = None,
    cls: list | None = None,
    cost: dict | None = None,
    shape: str | None = None,
    rows: int | None = None,
    engine_ms: float | None = None,
    materialized: str | None = None,
    tool: str = "run_sql",
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
        "audit": {
            "tool": tool,
            "principal": principal,
            "sql_sha256": hashlib.sha256(sql.encode()).hexdigest()[:16],
            "sql": sql[:SQL_MAX],
            "stage": stage,
            "reason": (reason or "")[:500] or None,
            # which frozen metric definitions produced the figure: the SQL above
            # is what was asked for, so without this the line cannot say which
            # version of a number the answer quoted
            "semantic": semantic or [],
            "governed": governed,
            "rls": rls or [],
            "cls": cls or [],
            "cost": cost or None,
            "shape": shape,
            "materialized": materialized,
        },
    }
    _session["statements"] += 1
    if outcome != "executed":
        _session["rejected"] += 1
    if materialized:
        _session["materialized"] += 1
    _session["engine_ms"] += float(engine_ms or 0.0)
    _session["rows"] += int(rows or 0)
    if ENABLED:
        try:
            sys.stdout.write(json.dumps(rec, default=str) + "\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001, S110 — auditing must never break the tool  # nosec B110
            pass
    return rec
