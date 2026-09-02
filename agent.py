"""DuckDB Data Analyst Agent — Bedrock (Claude) + in-process DuckDB over S3.

The analytics engine lives INSIDE the agent process. No cluster, no
warehouse, no ETL — the agent starts, attaches DuckDB (an embedded OLAP
engine), and queries data directly on S3. Access control is the platform's:
a scoped read-only IAM execution role bounds what the engine can read, and
a statement gate keeps agent-generated SQL read-only.

Runs in two modes:
  - AgentCore Runtime entrypoint (default): `app.run()` serves /invocations
  - Local CLI: `python agent.py "your question"`
"""

import json
import os
import re
import sys
import threading
import time

import duckdb
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool

from data_catalog import VIEW_DEFS, build_catalog_doc

MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6")
QUERY_TIMEOUT_ROWS = 200  # hard cap on rows returned to the model
MEMORY_LIMIT = os.environ.get("DUCKDB_MEMORY_LIMIT", "2GB")
# Optional catalog legs, each toggled by its own env var (empty = off).
# The raw-Parquet views are always on. All legs mount READ_ONLY.
S3_TABLES_ARN = os.environ.get("S3_TABLES_ARN", "")  # s3t: native Iceberg REST
GLUE_CATALOG = os.environ.get("GLUE_CATALOG", "")  # glue: '<acct>:s3tablescatalog/<bucket>'
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
        c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config, not user input
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
            c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config, not user input
                f"ATTACH 'ducklake:{DUCKLAKE_CATALOG}' AS dl (READ_ONLY, META_SECRET pgcat)"
            )
        else:
            c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config, not user input
                f"ATTACH 'ducklake:{DUCKLAKE_CATALOG}' AS dl (READ_ONLY)"
            )


def get_conn() -> duckdb.DuckDBPyConnection:
    """One in-process DuckDB per agent instance, lazily initialized."""
    global _conn
    if _conn is None:
        c = duckdb.connect()  # in-memory; nothing persisted
        # containers/SSM may run without a resolvable HOME; extensions need one
        c.execute("SET home_directory='/tmp'")
        c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config, not user input
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
            # VIEW_DEFS is a static module constant, not user input.
            c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- static constants
                f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {src}"
            )
        _attach_legs(c)
        _conn = c
    return _conn


# ---------------------------------------------------------------------- tools
def _gate(sql: str) -> str | None:
    """Return an error string if the statement is not allowed, else None."""
    normalized = " ".join(sql.strip().lower().split())
    if normalized.startswith(_READ_PREFIXES):
        return None
    if _TEMP_DDL.match(normalized):
        return None  # session-local temp table/view: in-memory, dies with process
    return (
        "ERROR: only SELECT / DESCRIBE / SUMMARIZE / WITH / SHOW / EXPLAIN or "
        "CREATE TEMP TABLE|VIEW ... AS SELECT are allowed."
    )


@tool
def run_sql(sql: str) -> str:
    """Run a DuckDB SQL statement and return rows as JSON (max 200) plus
    engine metrics ({"engine_ms", "rows"}). Allowed: read statements and
    CREATE TEMP TABLE/VIEW for building session working sets.

    Args:
        sql: A single DuckDB SQL statement.
    """
    err = _gate(sql)
    if err:
        return err
    t0 = time.perf_counter()
    try:
        with _conn_lock:  # serialize parallel tool calls; see _conn_lock note
            cur = get_conn().execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(QUERY_TIMEOUT_ROWS + 1) if cols else []
    except Exception as e:  # surface engine errors verbatim so the model can fix the SQL
        return f"ERROR: {e}"
    engine_ms = round((time.perf_counter() - t0) * 1000, 1)
    truncated = len(rows) > QUERY_TIMEOUT_ROWS
    rows = rows[:QUERY_TIMEOUT_ROWS]
    out = [dict(zip(cols, (str(v) for v in r), strict=False)) for r in rows]
    payload = json.dumps(out, ensure_ascii=False, default=str)
    payload += f'\n{{"engine_ms": {engine_ms}, "rows": {len(rows)}}}'
    if truncated:
        payload += f"\n[TRUNCATED at {QUERY_TIMEOUT_ROWS} rows — add aggregation or LIMIT]"
    return payload


SYSTEM_PROMPT = f"""You are a blockchain data analyst agent. You answer questions
by writing DuckDB SQL against open datasets on Amazon S3, using the run_sql tool.

{build_catalog_doc(active_legs())}

## Method
1. If unsure about a schema, DESCRIBE the single-day explicit path first.
2. Write one query at a time; read the result before deciding the next step.
3. If a query errors, read the error and fix your SQL — do not give up.
4. For multi-step analysis over the same subset, first build a session working
   set (CREATE TEMP TABLE ws AS SELECT ... WHERE date=...), then run follow-up
   queries against ws — this avoids re-scanning S3 on every question.
5. Answer in the language the user asked in. Show the key numbers AND the SQL
   you used, so the analysis is reproducible. Cite engine_ms when summarizing
   so the reader sees the engine-side latency.
6. Never query S3 tables without a date filter (temp tables are exempt)."""

agent = Agent(model=MODEL_ID, tools=[run_sql], system_prompt=SYSTEM_PROMPT)

# ------------------------------------------------------- AgentCore entrypoint
app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload, context=None):
    """AgentCore Runtime entrypoint: {"prompt": "..."} -> {"result": "..."}"""
    user_msg = payload.get("prompt", "Summarize yesterday's Bitcoin activity.")
    result = agent(user_msg)
    return {"result": result.message}


if __name__ == "__main__":
    if len(sys.argv) > 1:  # local one-shot: python agent.py "question"
        print(agent(" ".join(sys.argv[1:])))
    else:
        app.run()
