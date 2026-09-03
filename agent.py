"""DuckDB Data Analyst Agent — Bedrock (Claude) + in-process DuckDB over S3.

The analytics engine lives INSIDE the agent process. No cluster, no
warehouse, no ETL — the agent starts, attaches DuckDB (an embedded OLAP
engine), and queries data directly on S3. Governance is identity-aware:
every statement is rewritten under the caller's row/column policy before
the engine sees it (see governance.py).

Runs in two modes:
  - AgentCore Runtime entrypoint (default): `app.run()` serves /invocations
  - Local CLI: `python agent.py "your question"`
"""

import asyncio
import json
import os
import re
import sys
import threading
import time

import duckdb
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool

import governance
from data_catalog import VIEW_DEFS, build_catalog_doc, build_label_views

MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6")
QUERY_TIMEOUT_ROWS = 200  # hard cap on rows returned to the model
MEMORY_LIMIT = os.environ.get("DUCKDB_MEMORY_LIMIT", "2GB")
# Optional catalog legs, each toggled by its own env var (empty = off).
# The raw-Parquet views are always on. All legs mount READ_ONLY.
S3_TABLES_ARN = os.environ.get("S3_TABLES_ARN", "")  # s3t: native Iceberg REST
GLUE_CATALOG = os.environ.get("GLUE_CATALOG", "")  # glue: '<acct>:s3tablescatalog/<bucket>'
# Attribution label tables (ofac_addresses + cex_deposit_labels) under this
# s3:// prefix; empty = off (raw ledger only, no entity attribution).
LABELS_PREFIX = os.environ.get("LABELS_PREFIX", "")
# dl: DuckLake catalog. Two forms:
#   's3://.../catalog.ducklake'        — DuckDB-file catalog (single-writer)
#   'postgres:dbname=...'              — RDS PostgreSQL catalog (multi-client);
#     connection credentials come from Secrets Manager (DUCKLAKE_PG_SECRET)
DUCKLAKE_CATALOG = os.environ.get("DUCKLAKE_CATALOG", "")
DUCKLAKE_PG_SECRET = os.environ.get("DUCKLAKE_PG_SECRET", "duckdb-agent/ducklake-pg")

# session-local temp objects are the ONLY allowed writes: they live in the
# in-memory db and die with the process, so they carry no persistence risk.
_TEMP_DDL = re.compile(
    r"^create\s+(?:or\s+replace\s+)?temp(?:orary)?\s+(?:table|view)\s", re.IGNORECASE
)
_READ_PREFIXES = ("select", "describe", "summarize", "with", "show", "explain")

# ---------------------------------------------------------------- DuckDB setup
_conn = None
# The model may issue several run_sql calls in ONE response; Strands executes
# them on separate threads. DuckDB's Python connection holds a single active
# result set, so unsynchronized execute+fetch pairs steal each other's rows
# (observed in cloud: count(*) "returning" 0 rows). One lock serializes tool
# calls on the shared connection — required to keep temp tables session-visible
# (a cursor() per thread would give each thread its own temp namespace).
_conn_lock = threading.Lock()


def active_legs() -> list[str]:
    """Optional catalog legs enabled by env, in mount order."""
    legs = []
    if S3_TABLES_ARN:
        legs.append("s3t")
    if GLUE_CATALOG:
        legs.append("glue")
    if DUCKLAKE_CATALOG:
        legs.append("dl")
    return legs


def _attach_legs(c: duckdb.DuckDBPyConnection) -> None:
    """Mount each enabled catalog leg READ_ONLY under its fixed alias."""
    if S3_TABLES_ARN or GLUE_CATALOG:
        c.execute("INSTALL iceberg; LOAD iceberg;")
    if S3_TABLES_ARN:  # Iceberg REST, S3 Tables native endpoint
        c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config, not user input
            f"ATTACH '{S3_TABLES_ARN}' AS s3t (TYPE iceberg, ENDPOINT_TYPE s3_tables, READ_ONLY)"
        )
    if GLUE_CATALOG:  # Iceberg REST, Glue endpoint (federated s3tablescatalog)
        c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config
            f"ATTACH '{GLUE_CATALOG}' AS glue (TYPE iceberg, ENDPOINT_TYPE glue, READ_ONLY)"
        )
    if DUCKLAKE_CATALOG:  # SQL-database catalog; Parquet data on S3
        c.execute("INSTALL ducklake; LOAD ducklake;")
        if DUCKLAKE_CATALOG.startswith("postgres:"):
            # RDS PostgreSQL catalog: credentials from Secrets Manager, never env
            import boto3

            sec = json.loads(
                boto3.client("secretsmanager").get_secret_value(SecretId=DUCKLAKE_PG_SECRET)[
                    "SecretString"
                ]
            )
            c.execute("INSTALL postgres; LOAD postgres;")
            c.execute(
                "CREATE SECRET pgcat (TYPE postgres, "
                f"HOST '{sec['host']}', PORT {sec.get('port', 5432)}, "
                f"DATABASE '{sec['dbname']}', "
                f"USER '{sec['username']}', PASSWORD '{sec['password']}')"
            )
            c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config
                f"ATTACH 'ducklake:{DUCKLAKE_CATALOG}' AS dl (READ_ONLY, META_SECRET pgcat)"
            )
        else:
            c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config
                f"ATTACH 'ducklake:{DUCKLAKE_CATALOG}' AS dl (READ_ONLY)"
            )


def get_conn() -> duckdb.DuckDBPyConnection:
    """One in-process DuckDB per agent instance, lazily initialized."""
    global _conn
    if _conn is None:
        c = duckdb.connect()  # in-memory; nothing persisted
        # containers/SSM may run without a resolvable HOME; extensions need one
        c.execute("SET home_directory='/tmp'")
        c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config
            f"SET memory_limit='{MEMORY_LIMIT}'"
        )
        c.execute("SET enable_object_cache=true")
        c.execute("INSTALL httpfs; LOAD httpfs;")
        # Sign with whatever the environment provides (local profile or the
        # AgentCore execution role). The public dataset lives in us-east-2 and
        # gets a scoped secret; the unscoped default stays us-west-2 so the
        # catalog legs (managed storage / ducklake bucket) sign correctly.
        c.execute(
            "CREATE OR REPLACE SECRET aws_default ("
            "  TYPE s3, PROVIDER credential_chain, REGION 'us-west-2'"
            ")"
        )
        c.execute(
            "CREATE OR REPLACE SECRET blockchain ("
            "  TYPE s3, PROVIDER credential_chain, REGION 'us-east-2',"
            "  SCOPE 's3://aws-public-blockchain'"
            ")"
        )
        for name, src in VIEW_DEFS.items():
            c.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {src}")  # nosec B608 - name/src are VIEW_DEFS literals in data_catalog.py, not input  # nosemgrep: sqlalchemy-execute-raw-query -- static VIEW_DEFS literals
        for name, src in build_label_views(LABELS_PREFIX).items():
            c.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {src}")  # nosec B608 - name is a LABEL_TABLES literal, src derived from operator env  # nosemgrep: sqlalchemy-execute-raw-query -- label views from operator env
        _attach_legs(c)

        # governance CLS needs per-reference schemas (a logical table can
        # expose different columns per access method); probe via DESCRIBE.
        # The prober runs during apply_governance — outside run_sql's lock —
        # so it must take _conn_lock itself to keep the shared cursor safe.
        # Injection note (this one is NOT a constant, unlike the views above):
        # `ref` is model-influenced — it is sqlglot's re-serialization of a table
        # reference taken from agent-generated SQL, and the catalog/schema
        # qualifiers can be arbitrary identifiers. It is safe because sqlglot
        # emits identifiers quoted whenever quoting is required, so a payload
        # cannot leave the identifier. Checked against DuckDB's own splitter:
        #   FROM "a; DROP TABLE victim".btc_transactions
        #     -> DESCRIBE SELECT * FROM "a; DROP TABLE victim".btc_transactions
        #     -> extract_statements() == 1 statement
        # This holds only as long as the value comes from `Expression.sql()`.
        # Never interpolate a raw string from the model here.
        def _probe(ref: str) -> set:
            with _conn_lock:
                return {r[0] for r in c.execute(f"DESCRIBE SELECT * FROM {ref} LIMIT 0").fetchall()}  # nosec B608 - see injection note above  # nosemgrep: sqlalchemy-execute-raw-query -- sqlglot-reserialized identifier, read-only DESCRIBE

        governance.set_schema_prober(_probe)
        _conn = c
    return _conn


# ---------------------------------------------------------------------- tools
def _single_statement(sql: str) -> bool:
    """True if `sql` is exactly one statement.

    The gate matches on the leading keyword, so without this check a write
    rides in behind a read: `SELECT 1; DROP TABLE t` starts with "select",
    passes, and DuckDB's execute() runs BOTH statements. Verified: the table
    was dropped, and `SELECT 1; COPY t TO '/tmp/f.csv'` wrote the file — the
    exfiltration primitive the gate exists to block.

    Splitting is delegated to DuckDB's own parser rather than a hand-rolled
    scan for ';'. A textual scan has to know about single/double quotes, line
    and block comments and dollar-quoting to avoid both false positives
    (`WHERE txid = 'a;b'`) and false negatives, and any divergence from the
    engine's lexer is exactly the gap an attacker aims at. extract_statements
    uses the same parser that will execute the SQL, so there is nothing to
    diverge. Unparseable SQL is left to the engine, which is where the model's
    self-repair loop expects the error to come from.
    """
    try:
        return len(duckdb.extract_statements(sql)) <= 1
    except Exception:  # noqa: BLE001 — unparseable: DuckDB will reject it too
        return True


def _strip_leading_sql_comments(sql: str) -> str:
    """Drop leading -- line and /* */ block comments so the gate classifies on
    the first real keyword. Models routinely prefix a query with a comment;
    stripping cannot weaken the gate (a commented-out DROP still starts with
    DROP once the comment is removed, and the single-statement check runs on
    the ORIGINAL text)."""
    s = sql.lstrip()
    while True:
        if s.startswith("--"):
            nl = s.find("\n")
            s = "" if nl == -1 else s[nl + 1 :]
        elif s.startswith("/*"):
            end = s.find("*/")
            s = "" if end == -1 else s[end + 2 :]
        else:
            return s
        s = s.lstrip()


def _gate(sql: str) -> str | None:
    """Return an error string if the statement is not allowed, else None."""
    normalized = " ".join(_strip_leading_sql_comments(sql).lower().split())
    if not _single_statement(sql):
        return (
            "ERROR: submit one statement per call — multi-statement SQL is not "
            "allowed (the read-only gate applies to the leading statement only)."
        )
    if normalized.startswith(_READ_PREFIXES):
        return None
    if _TEMP_DDL.match(normalized):
        return None  # session-local temp table/view: in-memory, dies with process
    return (
        "ERROR: only SELECT / DESCRIBE / SUMMARIZE / WITH / SHOW / EXPLAIN or "
        "CREATE TEMP TABLE|VIEW ... AS SELECT are allowed."
    )


def _execute_governed(governed_sql: str) -> tuple[str, float, int, bool]:
    """Run an already-governed statement under the shared-connection lock.

    Returns (rows_json, engine_ms, n_rows, truncated). Raises on engine error.
    The `_conn_lock` serialization (invariant #6) is kept exactly — a per-thread
    cursor would hide session temp tables from later calls.
    """
    t0 = time.perf_counter()
    with _conn_lock:  # serialize parallel tool calls; see _conn_lock note
        cur = get_conn().execute(governed_sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(QUERY_TIMEOUT_ROWS + 1) if cols else []
    engine_ms = round((time.perf_counter() - t0) * 1000, 1)
    truncated = len(rows) > QUERY_TIMEOUT_ROWS
    rows = rows[:QUERY_TIMEOUT_ROWS]
    out = [dict(zip(cols, (str(v) for v in r), strict=False)) for r in rows]
    return json.dumps(out, ensure_ascii=False, default=str), engine_ms, len(rows), truncated


def _format_result(
    rows_json: str, engine_ms: float, n_rows: int, truncated: bool, governed: bool
) -> str:
    """The exact string the model reads: rows line, metrics line, truncation marker."""
    payload = rows_json
    payload += (
        f'\n{{"engine_ms": {engine_ms}, "rows": {n_rows}, "governed": {str(governed).lower()}}}'
    )
    if truncated:
        payload += f"\n[TRUNCATED at {QUERY_TIMEOUT_ROWS} rows — add aggregation or LIMIT]"
    return payload


def _run_sql_core(sql: str) -> str:
    """Synchronous gate → govern → execute → format. Directly callable and
    fully unit-tested; the streaming run_sql tool shares these same helpers so
    the model sees an identical result string whether or not the UI streams."""
    err = _gate(sql)
    if err:
        return err
    try:
        # authorization: rewrite under the caller's policy BEFORE the engine
        # sees the statement (RLS predicate + column exclusion; raw-path and
        # unparseable SQL rejected for restricted principals — fail closed)
        governed_sql = governance.apply_governance(sql)
    except ValueError as e:
        return f"ERROR: {e}"
    try:
        rows_json, engine_ms, n_rows, truncated = _execute_governed(governed_sql)
    except Exception as e:  # surface engine errors verbatim so the model can fix the SQL
        return f"ERROR: {e}"
    return _format_result(rows_json, engine_ms, n_rows, truncated, governed_sql != sql)


@tool
async def run_sql(sql: str):
    """Run a DuckDB SQL statement and return rows as JSON (max 200) plus
    engine metrics ({"engine_ms", "rows"}). Allowed: read statements and
    CREATE TEMP TABLE/VIEW for building session working sets.

    Args:
        sql: A single DuckDB SQL statement.
    """
    # This is an async-generator tool: intermediate yields surface to the UI as
    # tool_stream events (the live gate → rewrite → execute trace), while the
    # FINAL yield becomes the ToolResult the model reads — identical to the
    # sync path, so agent behaviour is unchanged. The engine is in-process:
    # each phase carries the evidence (the rewritten SQL, the engine_ms) with
    # zero network hops to a query service.
    err = _gate(sql)
    if err:
        yield {"phase": "gate", "allowed": False, "sql": sql[:400]}
        yield err
        return
    yield {"phase": "gate", "allowed": True, "sql": sql[:400]}

    try:
        governed_sql = governance.apply_governance(sql)
    except ValueError as e:
        yield {"phase": "rewrite", "rejected": True, "reason": str(e)}
        yield f"ERROR: {e}"
        return
    governed = governed_sql != sql
    applied = governance.describe_applied(sql)
    yield {
        "phase": "rewrite",
        "governed": governed,
        "principal": governance.active_role(),
        "rls": applied["rls"],
        "cls": applied["cls"],
        "rewritten_sql": governed_sql[:600] if governed else None,
    }

    # run the blocking execute off the event loop; the threading lock still
    # serializes across the worker thread, keeping temp tables session-visible
    try:
        rows_json, engine_ms, n_rows, truncated = await asyncio.to_thread(
            _execute_governed, governed_sql
        )
    except Exception as e:  # noqa: BLE001 — surface engine errors for self-repair
        yield {"phase": "execute", "error": str(e)}
        yield f"ERROR: {e}"
        return
    yield {
        "phase": "execute",
        "engine": "in-process duckdb",
        "engine_ms": engine_ms,
        "rows": n_rows,
        "truncated": truncated,
    }
    # final yield → the ToolResult the model reads (same format as the sync core)
    yield _format_result(rows_json, engine_ms, n_rows, truncated, governed)


SYSTEM_PROMPT = f"""You are an on-chain investigations analyst. You trace flows of
funds and screen sanctions exposure on public Ethereum data by writing DuckDB
SQL against Amazon S3, using the run_sql tool. Your users are exchange security
and AML/compliance teams working active cases.

{build_catalog_doc(active_legs(), labels=bool(LABELS_PREFIX))}

## How on-chain investigation works (your method)
1. ANCHOR on the address/tx the user gives you. If unsure about a schema,
   DESCRIBE the single-day explicit path first.
2. A contract-mediated drain does NOT appear in eth_transactions — follow the
   money through eth_traces (internal calls). Filter by to_address (inbound)
   or from_address (outbound) plus the date.
3. BUILD THE CASE WORKING SET ONCE: CREATE TEMP TABLE case_flows AS SELECT ...
   the filtered slice (one S3 scan), then run every follow-up (counts,
   distributions, equal-amount fan-out, top recipients) against the in-memory
   temp table — never re-scan S3 per question.
4. HOP by recursion: take the recipients from the working set and run the same
   trace query with them as from_address to reach the next layer; append to a
   TEMP TABLE. Each hop's query depends on the previous hop's result.
5. ATTRIBUTE: join addresses against ofac_addresses (sanctions exposure) and
   cex_deposit_labels (cash-out points) on lower(address).
6. Write one query at a time; read the result before deciding the next step.
   If a query errors, read the error and fix your SQL — do not give up.
7. Answer in the language the user asked in. Show the key numbers AND the SQL
   you used, so the case is reproducible. Cite engine_ms when summarizing so
   the reader sees the in-process engine-side latency.
8. Never scan a raw S3 table without a date filter or an address filter (temp
   tables are exempt)."""  # nosec B608 - system-prompt text (mentions SQL), not query construction

agent = Agent(model=MODEL_ID, tools=[run_sql], system_prompt=SYSTEM_PROMPT)

# ------------------------------------------------------- AgentCore entrypoint
app = BedrockAgentCoreApp()


def _resolve_claims(payload, context) -> dict | None:
    """Extract identity claims for governance, most-trusted source first.

    1. JWT claims — production path: the inbound JWT authorizer has already
       validated the token before this code runs; with the Authorization
       header allowlisted, decode it here (signature re-check unnecessary).
    2. X-Amzn-Bedrock-AgentCore-Runtime-User-Id header — SigV4 path, set by
       the calling application after ITS OWN authentication of the end user.
    3. payload "identity" object — for callers that pass tenant/role inline
       (the SigV4 caller is IAM-authenticated infrastructure; it is trusted
       to assert its users' identity, same trust model as the header).
    """
    headers = getattr(context, "request_headers", None) or {}
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            import jwt  # PyJWT; validated upstream by the inbound authorizer

            # Deliberate, and the reason is the architecture: AuthN is delegated
            # to AgentCore's managed inbound JWT authorizer, which verifies the
            # signature BEFORE this process is reached (threat model M005). This
            # call is claims EXTRACTION from an already-verified token, not
            # authentication. Re-verifying here would mean shipping JWKS
            # fetching, key rotation and clock handling into the agent for no
            # added assurance. 🔴 If you deploy WITHOUT the inbound authorizer,
            # this line trusts the caller completely — see threats T004/M006.
            claims_only = {"verify_signature": False}  # nosemgrep: unverified-jwt-decode
            decoded = jwt.decode(auth[7:], options=claims_only)  # nosemgrep: unverified-jwt-decode
            # normalize IdP claim names → {tenant, role}. Cognito custom
            # attributes arrive as custom:tenant / custom:role; other IdPs may
            # send bare tenant/role. resolve_role() reads {tenant, role}.
            return {
                "tenant": decoded.get("custom:tenant") or decoded.get("tenant", ""),
                "role": decoded.get("custom:role") or decoded.get("role", ""),
            }
        except Exception:  # noqa: BLE001 — fall through to weaker sources
            pass
    user_id = headers.get("X-Amzn-Bedrock-AgentCore-Runtime-User-Id")
    if user_id and ":" in user_id:  # convention: "<tenant>:<role>"
        tenant, role = user_id.split(":", 1)
        return {"tenant": tenant, "role": role}
    identity = payload.get("identity")
    if isinstance(identity, dict):
        return identity
    return None


def _envelope(kind: str, **fields) -> dict:
    """One typed SSE frame. `kind` drives the UI's rendering switch."""
    return {"kind": kind, **fields}


def _map_event(ev: dict, trace: list) -> dict | None:
    """Translate one Strands stream event into a typed UI envelope, and
    accumulate the per-SQL gate→rewrite→execute trace for the final result.

    Strands event shapes (strands-agents 1.54):
      {"data": text, ...}                        -> assistant text delta
      {"reasoningText": text, "reasoning": True} -> chain-of-thought delta
      {"current_tool_use": {toolUseId,name,input}} -> tool call assembling
      {"tool_stream_event": {tool_use, data}}    -> our run_sql phase yields
    """
    if "reasoningText" in ev:
        return _envelope("reasoning", text=ev["reasoningText"])
    if "data" in ev and isinstance(ev.get("data"), str) and "tool_stream_event" not in ev:
        return _envelope("text", text=ev["data"])
    tse = ev.get("tool_stream_event")
    if tse and isinstance(tse.get("data"), dict) and "phase" in tse["data"]:
        phase = tse["data"]
        trace.append(phase)  # consolidated trace for the final result event
        return _envelope("trace", **phase)
    ctu = ev.get("current_tool_use")
    if ctu and ctu.get("name"):
        return _envelope(
            "tool_start", tool=ctu["name"], tool_use_id=ctu.get("toolUseId"), input=ctu.get("input")
        )
    return None


@app.entrypoint
async def invoke(payload, context=None):
    """AgentCore Runtime entrypoint (streaming).

    Yields typed SSE envelopes so a UI can render the run live:
      {"kind":"reasoning"|"text", "text": ...}  — thinking / answer deltas
      {"kind":"tool_start", "tool":"run_sql", "input": ...}
      {"kind":"trace", "phase":"gate"|"rewrite"|"execute", ...}  — in-process
        DuckDB pipeline, one set per SQL statement
      {"kind":"result", "principal": ..., "trace":[...all phases...]}
    The consolidated `trace` array in the final event backs a Trace tab.
    """
    claims = _resolve_claims(payload, context)
    governance.set_active_role(governance.resolve_role(claims))
    user_msg = payload.get(
        "prompt", "Trace the flow of funds for the incident under investigation."
    )

    trace: list = []
    yield _envelope("start", principal=governance.active_role())
    async for ev in agent.stream_async(user_msg):
        if not isinstance(ev, dict):
            continue
        env = _map_event(ev, trace)
        if env is not None:
            yield env
    yield _envelope("result", principal=governance.active_role(), trace=trace)


if __name__ == "__main__":
    if len(sys.argv) > 1:  # local one-shot: python agent.py "question"
        print(agent(" ".join(sys.argv[1:])))
    else:
        app.run()
