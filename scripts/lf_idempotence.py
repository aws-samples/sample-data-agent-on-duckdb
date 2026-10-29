#!/usr/bin/env python3
"""Lake Formation idempotence check: the same person, the
same question, two engines — row-for-row equal or the check is red.

Side A is this repository: the in-process DuckDB engine with Lake Formation's
filters COMPILED INTO the statement by the rewrite layer (the distributed
enforcement model's "registered engine filters itself, fail-closed" step).
Side B is Athena, where Lake Formation enforces the same filters NATIVELY.
Both sides run as the SAME Lake Formation principal — the persona's data
access role, assumed with the `LakeFormationAuthorizedCaller` session tag —
so a difference can only come from the compilation, which is the thing under
test. Governance never gets the benefit of the doubt: a case that cannot be
run on either side is a FAILURE, not a skip.

Must run IN-REGION (the engine mounts S3 views; a laptop times out). On an
in-region host:

    ./.venv/bin/python scripts/lf_idempotence.py            # every case
    ./.venv/bin/python scripts/lf_idempotence.py --persona junior
    ./.venv/bin/python scripts/lf_idempotence.py --out /tmp/idem.md

Requires `LF_GOVERNANCE` (same value the runtime carries), optionally
`ATHENA_OUTPUT` (an S3 prefix for Athena results; default: the stack's
DuckLake bucket) and Athena permissions on the persona data
access roles (run queries in the workgroup, write to that prefix); the caller
must be trusted to assume those roles with the session tag.
Exit code 1 on any mismatch, so it can gate a deployment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")
# results land under the solution bucket unless told otherwise; the account id
# comes from the caller's identity, never from code (public-tree hygiene)
ATHENA_OUTPUT = os.environ.get("ATHENA_OUTPUT", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")

# The governed table, as each side names it. Side A reads the S3 Tables leg
# (`s3t.*`) so both sides read the SAME physical table; the policy matches the
# bare logical name, so the rewrite applies identically on either path.
DUCKDB_TABLE = "s3t.blockchain.eth_traces"
ATHENA_TABLE = '"s3tablescatalog/data-agent-demo"."blockchain"."eth_traces"'

# Ten questions chosen to exercise what a compiled filter can get WRONG:
# the row filter's boundary (inside, on, outside the window), its interaction
# with the caller's own predicates (narrower, wider, contradictory), the
# column projection a CLS rule removes, grouping and ordering over filtered
# rows, and an aggregate that the catalog could answer from metadata if the
# filter were dropped. `{t}` is the table, `{cols}` the projection.
CASES: list[dict] = [
    {
        "id": "row-count-whole-table",
        "why": "the filter must bound an unqualified count — the headline number",
        "sql": "SELECT count(*) AS n FROM {t}",
    },
    {
        "id": "row-count-inside-window",
        "why": "a caller predicate strictly inside the filter window composes, not conflicts",
        "sql": "SELECT count(*) AS n FROM {t} WHERE date = '2025-02-22'",
    },
    {
        "id": "row-count-on-boundary",
        "why": "the filter's own boundary day (>= 2025-02-21) is inclusive on both sides",
        "sql": "SELECT count(*) AS n FROM {t} WHERE date = '2025-02-21'",
    },
    {
        "id": "row-count-outside-window",
        "why": "a caller predicate outside the window yields zero, never rows",
        "sql": "SELECT count(*) AS n FROM {t} WHERE date = '2024-01-01'",
    },
    {
        "id": "row-count-wider-than-window",
        "why": "a caller range WIDER than the filter is narrowed by it, not the other way round",
        "sql": "SELECT count(*) AS n FROM {t} WHERE date BETWEEN '2020-01-01' AND '2030-01-01'",
    },
    {
        "id": "group-by-day",
        "why": "per-partition counts expose a window off by one day",
        "sql": "SELECT date, count(*) AS n FROM {t} GROUP BY date ORDER BY date",
    },
    {
        "id": "sum-value",
        "why": "a measure over filtered rows: one extra row changes the sum",
        "sql": "SELECT round(sum(value) / 1e18, 4) AS eth FROM {t}",
    },
    {
        "id": "distinct-senders-day",
        "why": "cardinality over a filtered day, not just row counts",
        "sql": (
            "SELECT count(DISTINCT from_address) AS senders FROM {t} WHERE date = '2025-02-21'"
        ),
    },
    {
        "id": "top-recipients",
        "why": "ordering + limit over filtered rows (the shape an investigator actually asks for)",
        "sql": (
            "SELECT to_address, count(*) AS n FROM {t} "
            "WHERE date = '2025-02-21' GROUP BY to_address ORDER BY n DESC, to_address LIMIT 5"
        ),
    },
    {
        "id": "projection-of-authorized-columns",
        "why": (
            "the column list LF authorizes: junior loses block_timestamp, so the "
            "projection itself must differ by persona and still agree across engines"
        ),
        "sql": "SELECT {cols} FROM {t} WHERE date = '2025-02-21' ORDER BY value DESC LIMIT 3",
        "projection": True,
    },
]

PERSONAS = {
    "investigator": "exchange-a:investigator",
    "junior": "exchange-a:junior",
}


# --------------------------------------------------------------- side A (ours)
def side_a(role: str, sql: str) -> list[list]:
    """Run through the statement pipeline as the persona: LF policy bound at
    session start, filters compiled into the SQL by the rewrite layer."""
    from data_agent import governance, pipeline

    governance.set_active_role(role)
    governance.bind_lf_policy(role)
    out = pipeline.run_text(sql)
    first = out.split("\n", 1)[0]
    try:
        rows = json.loads(first)
    except json.JSONDecodeError:
        raise RuntimeError(f"side A did not return rows: {out[:300]}") from None
    if not isinstance(rows, list):
        raise RuntimeError(f"side A returned a summary, not rows: {first[:300]}")
    from data_agent.context import results

    return [[results.unmark(v) for v in r.values()] for r in rows]


def authorized_columns(role: str) -> list[str]:
    """The columns LF authorizes for this persona, in catalog order — the
    projection side A and side B must agree on."""
    from data_agent import governance
    from data_agent.compute import engine

    governance.set_active_role(role)
    governance.bind_lf_policy(role)
    # denied_columns() is {logical table → denied column names}; the policy
    # keys the governed table by its bare name on every access path
    denied = set(governance.denied_columns().get("eth_traces", frozenset()))
    with engine.conn_lock:
        cur = engine.get_conn().execute(  # nosemgrep: sqlalchemy-execute-raw-query -- constant
            f"DESCRIBE {DUCKDB_TABLE}"  # nosec B608 - fixed table name
        )
        cols = [r[0] for r in cur.fetchall()]
    return [c for c in cols if c.lower() not in denied]


# ------------------------------------------------------------- side B (Athena)
def _persona_session(role: str):
    import boto3

    cfg = json.loads(os.environ["LF_GOVERNANCE"])
    arn = cfg["roles"][role]
    cred = boto3.client("sts", region_name=REGION).assume_role(
        RoleArn=arn,
        RoleSessionName="lf-idempotence",
        Tags=[{"Key": "LakeFormationAuthorizedCaller", "Value": cfg["session_tag"]}],
        DurationSeconds=3600,
    )["Credentials"]
    return boto3.client(
        "athena",
        region_name=REGION,
        aws_access_key_id=cred["AccessKeyId"],
        aws_secret_access_key=cred["SecretAccessKey"],
        aws_session_token=cred["SessionToken"],
    )


def side_b(athena, sql: str, timeout_s: int = 300) -> list[list]:
    """Run the same question on Athena as the same tagged persona; Lake
    Formation applies its filters natively there."""
    qid = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=ATHENA_WORKGROUP,
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
    )["QueryExecutionId"]
    deadline = time.time() + timeout_s
    while True:
        ex = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = ex["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.time() > deadline:
            raise RuntimeError(f"athena query {qid} still {state} after {timeout_s}s")
        time.sleep(1.5)  # nosemgrep: arbitrary-sleep -- Athena poll backoff
    if state != "SUCCEEDED":
        raise RuntimeError(
            f"athena query {state}: {ex['Status'].get('StateChangeReason', '')[:300]}"
        )
    rows: list[list] = []
    token = None
    while True:
        kw = {"QueryExecutionId": qid, "MaxResults": 1000}
        if token:
            kw["NextToken"] = token
        page = athena.get_query_results(**kw)
        for r in page["ResultSet"]["Rows"]:
            rows.append([c.get("VarCharValue") for c in r["Data"]])
        token = page.get("NextToken")
        if not token:
            break
    return rows[1:] if rows else rows  # drop Athena's header row


# ------------------------------------------------------------------- compare
def _norm(cell) -> str:
    """One scalar, as text, so a DuckDB BIGINT and an Athena string compare.
    Numbers are compared by value (1 == 1.0 == '1'), everything else as text."""
    if cell is None:
        return "∅"
    s = str(cell).strip()
    try:
        f = float(s)
    except ValueError:
        return s.lower()
    return f"{f:.6f}" if f % 1 else str(int(f))


def compare(a: list[list], b: list[list]) -> str | None:
    """None when equal, else the first difference in words."""
    if len(a) != len(b):
        return f"row count {len(a)} (ours) vs {len(b)} (athena)"
    for i, (ra, rb) in enumerate(zip(a, b, strict=True)):
        if len(ra) != len(rb):
            return f"row {i}: {len(ra)} columns (ours) vs {len(rb)} (athena)"
        na, nb = [_norm(c) for c in ra], [_norm(c) for c in rb]
        if na != nb:
            return f"row {i}: {na} (ours) vs {nb} (athena)"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", action="append", choices=sorted(PERSONAS), default=[])
    ap.add_argument("--only", default="", help="comma-separated case ids")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if not os.environ.get("LF_GOVERNANCE"):
        sys.exit("LF_GOVERNANCE is required (the same value the runtime carries)")
    global ATHENA_OUTPUT
    if not ATHENA_OUTPUT:
        import boto3

        acct = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
        ATHENA_OUTPUT = f"s3://duckdb-agent-ducklake-{acct}/tmp/athena-idempotence/"  # nosec B108 - an S3 key prefix, not a local path
    personas = args.persona or sorted(PERSONAS)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    cases = [c for c in CASES if not only or c["id"] in only]

    rows_out: list[dict] = []
    for pname in personas:
        role = PERSONAS[pname]
        athena = _persona_session(role)
        cols = authorized_columns(role)
        projection = ", ".join(cols[:4])
        print(f"\n=== persona {pname} ({role}) · {len(cols)} authorized columns")
        for case in cases:
            sql_a = case["sql"].format(t=DUCKDB_TABLE, cols=projection)
            sql_b = case["sql"].format(t=ATHENA_TABLE, cols=projection)
            try:
                a = side_a(role, sql_a)
                b = side_b(athena, sql_b)
                diff = compare(a, b)
            except Exception as e:  # noqa: BLE001 — an unrunnable case is a FAILURE
                diff = f"error: {str(e)[:300]}"
                a = b = []
            ok = diff is None
            print(f"  {'PASS' if ok else 'FAIL'} {case['id']}: {diff or f'{len(a)} rows agree'}")
            rows_out.append(
                {
                    "persona": pname,
                    "case": case["id"],
                    "why": case["why"],
                    "rows": len(a),
                    "ok": ok,
                    "detail": diff or "",
                }
            )

    passed = sum(1 for r in rows_out if r["ok"])
    total = len(rows_out)
    print(f"\nidempotence: {passed}/{total} comparisons agree")
    if args.out:
        lines = [
            "# Lake Formation idempotence check",
            "",
            f"{passed}/{total} comparisons agree — same persona, same question, "
            "in-process DuckDB with the filters compiled into the statement vs "
            "Athena with Lake Formation enforcing natively.",
            "",
            "| Persona | Case | Rows | Result | What it would catch |",
            "|---|---|---:|---|---|",
        ]
        lines += [
            f"| {r['persona']} | `{r['case']}` | {r['rows']} | "
            f"{'✅' if r['ok'] else '❌ ' + r['detail']} | {r['why']} |"
            for r in rows_out
        ]
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"→ {args.out}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
