"""The in-process engine: one DuckDB connection per agent instance.

In-memory, nothing persisted, dies with the session (invariant 2). Resource
envelope via DUCKDB_MEMORY_LIMIT (invariant 3). Every catalog leg mounts
READ_ONLY; every scenario view is mounted at init from the Storage registry.
"""

from __future__ import annotations

import os
import threading

import duckdb

from data_agent.storage import legs, registry

MEMORY_LIMIT = os.environ.get("DUCKDB_MEMORY_LIMIT", "2GB")

_conn: duckdb.DuckDBPyConnection | None = None
# lenient-scenario views that could not be mounted at init (name → first error line)
VIEW_ERRORS: dict[str, str] = {}
# The model may issue several run_sql calls in ONE response; Strands executes
# them on separate threads. DuckDB's Python connection holds a single active
# result set, so unsynchronized execute+fetch pairs steal each other's rows
# (observed in cloud: count(*) "returning" 0 rows). One lock serializes tool
# calls on the shared connection — required to keep temp tables session-visible
# (a cursor() per thread would give each thread its own temp namespace).
conn_lock = threading.Lock()


def get_conn() -> duckdb.DuckDBPyConnection:
    """One in-process DuckDB per agent instance, lazily initialized."""
    global _conn
    if _conn is None:
        c = duckdb.connect()  # in-memory; nothing persisted
        _configure(c)
        _s3_secrets(c)
        _mount_scenarios(c)
        legs.attach(c)
        _conn = c
    return _conn


def _configure(c: duckdb.DuckDBPyConnection) -> None:
    """Engine settings and the S3 extension."""
    # containers/SSM may run without a resolvable HOME; extensions need one
    c.execute("SET home_directory='/tmp'")
    c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config
        f"SET memory_limit='{MEMORY_LIMIT}'"
    )
    c.execute("SET enable_object_cache=true")
    c.execute("INSTALL httpfs; LOAD httpfs;")


def _s3_secrets(c: duckdb.DuckDBPyConnection) -> None:
    """Sign with whatever the environment provides (local profile or the
    AgentCore execution role). The public dataset lives in us-east-2 and gets
    a scoped secret; the unscoped default stays us-west-2 so the catalog legs
    (managed storage / ducklake bucket) sign correctly.

    DuckDB validates the credential chain when the secret is created, so this
    is the one init step that needs AWS credentials — tests that exercise
    engine init against local files stub it out."""
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


def _mount_scenarios(c: duckdb.DuckDBPyConnection) -> None:
    """Views and macros of every enabled scenario. A lenient scenario's view
    over a dataset that has not landed yet is skipped and recorded — DuckDB
    binds a view at creation, and an optional dataset must not take the whole
    engine down with it."""
    for scenario in registry.enabled():
        for name, src in scenario.views.items():
            stmt = f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {src}"  # nosec B608 - name/src are scenario literals derived from operator env, not model input
            if not scenario.lenient:
                c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- static scenario literals
                    stmt
                )
                continue
            try:
                c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- operator-env views
                    stmt
                )
            except Exception as e:  # noqa: BLE001 — missing/unreadable dataset
                VIEW_ERRORS[name] = str(e).split("\n")[0][:200]
        for macro in scenario.macros:
            c.execute(macro)  # nosemgrep: sqlalchemy-execute-raw-query -- operator env


def probe_columns(ref: str) -> set[str]:
    """Schema prober for governance CLS: the columns a relation exposes.

    Runs during apply_governance — outside the pipeline's execute lock — so it
    takes `conn_lock` itself to keep the shared cursor safe.

    Injection note (this one is NOT a constant, unlike the views above): `ref`
    is model-influenced — it is sqlglot's re-serialization of a table reference
    taken from agent-generated SQL, and the catalog/schema qualifiers can be
    arbitrary identifiers. It is safe because sqlglot emits identifiers quoted
    whenever quoting is required, so a payload cannot leave the identifier.
    Checked against DuckDB's own splitter:
      FROM "a; DROP TABLE victim".btc_transactions
        -> DESCRIBE SELECT * FROM "a; DROP TABLE victim".btc_transactions
        -> extract_statements() == 1 statement
    This holds only as long as the value comes from `Expression.sql()`.
    Never interpolate a raw string from the model here.
    """
    with conn_lock:
        rows = get_conn().execute(f"DESCRIBE SELECT * FROM {ref} LIMIT 0").fetchall()  # nosec B608 - see injection note above  # nosemgrep: sqlalchemy-execute-raw-query -- sqlglot-reserialized identifier, read-only DESCRIBE
        return {r[0] for r in rows}


def session_objects() -> set[str]:
    """Names of the session's temp tables/views — the objects a principal
    created in-session (working sets, framework `_r_<n>` results).

    Governance's table-level deny-by-default (rewrite.py) lets a restricted
    principal read these: they hold only rows the principal was already
    governed into (the temp table is built from the GOVERNED statement), so
    reading them cannot widen access. Only TEMPORARY objects qualify — the
    mounted scenario views are durable and are governed by the policy, never
    exempted here. Runs outside the pipeline lock, so it takes conn_lock."""
    with conn_lock:
        rows = get_conn().execute(
            "SELECT table_name FROM duckdb_tables() WHERE temporary "
            "UNION ALL SELECT view_name FROM duckdb_views() WHERE temporary"
        ).fetchall()
        return {r[0] for r in rows}


def reset() -> None:
    """Forget the connection (tests, the eval runner): the next get_conn()
    starts a fresh engine with no temp tables."""
    global _conn
    _conn = None
    VIEW_ERRORS.clear()
