"""The in-process engine: one DuckDB connection per agent instance.

In-memory, nothing persisted, dies with the session (invariant 2). Resource
envelope via DUCKDB_MEMORY_LIMIT (invariant 3). Every catalog leg mounts
READ_ONLY; every scenario view is mounted at init from the Storage registry.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import threading
import time

import duckdb

from data_agent.storage import legs, registry

MEMORY_LIMIT = os.environ.get("DUCKDB_MEMORY_LIMIT", "2GB")
# Per-statement evidence from the engine's own instrumentation, zero
# network — JSON profiling (TOTAL_BYTES_READ) written here per statement, and
# the HTTP log (one row per request httpfs makes) counted per statement.
_PROFILE_PATH = os.path.join(tempfile.gettempdir(), f"duckdb-profile-{os.getpid()}.json")
_stats = {"enabled": False}
# Where the pre-installed core extensions live (baked into the image by
# scripts/install_extensions.py). Empty = DuckDB's default under the
# home directory. The engine only ever LOADs; it never INSTALLs at cold start.
EXTENSION_DIR = os.environ.get("DUCKDB_EXTENSION_DIR", "")
# httpfs for S3; aws for the credential_chain secret provider (a separate
# extension — DuckDB 2.0 no longer auto-installs it). Legs LOAD iceberg /
# ducklake, the data map LOADs fts.
CORE_EXTENSIONS = ("httpfs", "aws")

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
        t0 = time.perf_counter()
        c = duckdb.connect()  # in-memory; nothing persisted
        _configure(c)
        _s3_secrets(c)
        _mount_scenarios(c)
        legs.attach(c)
        _conn = c
        # one line per cold start, on stdout → CloudWatch: the evidence that
        # init took N ms and loaded (not downloaded) its extensions
        print(
            json.dumps(
                {
                    "engine": "cold start",
                    "duckdb": duckdb.__version__,
                    "init_ms": round((time.perf_counter() - t0) * 1000),
                    "extensions_loaded": _loaded(c),
                    "install_at_runtime": False,
                    "view_errors": sorted(VIEW_ERRORS),
                }
            ),
            flush=True,
        )
    return _conn


def _loaded(c: duckdb.DuckDBPyConnection) -> list[str]:
    try:
        return sorted(
            r[0]
            for r in c.execute(
                "SELECT extension_name FROM duckdb_extensions() WHERE loaded"
            ).fetchall()
        )
    except Exception:  # noqa: BLE001 — diagnostics only
        return []


def _configure(c: duckdb.DuckDBPyConnection) -> None:
    """Engine settings and the S3 extension (LOAD only — never INSTALL)."""
    # containers/SSM may run without a resolvable HOME; extensions need one
    c.execute("SET home_directory='/tmp'")
    if EXTENSION_DIR:
        c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config
            f"SET extension_directory='{EXTENSION_DIR}'"
        )
    # No cold-start download from the extension repository (threat model T010): the
    # image ships the extensions; a missing one is a build error, not a fetch.
    c.execute("SET autoinstall_known_extensions=false")
    c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config
        f"SET memory_limit='{MEMORY_LIMIT}'"
    )
    c.execute("SET enable_object_cache=true")
    for ext in CORE_EXTENSIONS:
        load_extension(c, ext)
    enable_statement_stats(c)


def enable_statement_stats(c: duckdb.DuckDBPyConnection) -> None:
    """Turn on the two in-process evidence sources for the audit line:
    JSON profiling with TOTAL_BYTES_READ (written to a temp file per statement;
    coverage ALL so a CTAS profiles too) and the HTTP log (one `duckdb_logs`
    row per request httpfs makes — the S3 request count). Fail-soft: a build
    without either facility just leaves the audit fields empty."""
    try:
        c.execute("PRAGMA enable_profiling='json'")
        c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- process-local temp path
            f"PRAGMA profile_output='{_PROFILE_PATH}'"
        )
        c.execute("PRAGMA profiling_coverage='ALL'")
        c.execute('PRAGMA custom_profiling_settings=\'{"TOTAL_BYTES_READ": "true"}\'')
        c.execute("CALL enable_logging('HTTP')")
        _stats["enabled"] = True
    except Exception:  # noqa: BLE001 — evidence is optional, the engine is not
        _stats["enabled"] = False


def begin_statement_stats(conn: duckdb.DuckDBPyConnection) -> None:
    """Reset the per-statement counters. Caller holds `conn_lock` and runs the
    statement next, so everything counted afterwards belongs to it."""
    if not _stats["enabled"]:
        return
    try:
        conn.execute("CALL truncate_duckdb_logs()")
        if os.path.exists(_PROFILE_PATH):
            os.unlink(_PROFILE_PATH)
    except Exception:  # noqa: BLE001 — evidence must never break the statement
        pass


def collect_statement_stats(conn: duckdb.DuckDBPyConnection) -> dict:
    """Read the statement's evidence right AFTER it ran (caller still holds the
    lock, before any framework statement overwrites the profile): bytes the
    engine read and the number of HTTP requests httpfs made. A local-table
    statement correctly reports 0 requests — the marginal-cost story."""
    out: dict = {"bytes_scanned": None, "s3_requests": None}
    if not _stats["enabled"]:
        return out
    try:  # the profile file first — our own count query below would overwrite it
        with open(_PROFILE_PATH, encoding="utf-8") as f:
            out["bytes_scanned"] = int(json.load(f).get("total_bytes_read") or 0)
    except Exception:  # noqa: BLE001 — a statement outside profiling coverage
        pass
    try:
        out["s3_requests"] = int(
            conn.execute("SELECT count(*) FROM duckdb_logs WHERE type='HTTP'").fetchone()[0]
        )
    except Exception:  # noqa: BLE001
        pass
    return out


def load_extension(c: duckdb.DuckDBPyConnection, name: str) -> None:
    """LOAD a pre-installed extension; a missing one fails with the fix, not a download."""
    try:
        c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- fixed extension names
            f"LOAD {name}"
        )
    except duckdb.Error as e:
        raise RuntimeError(
            f"DuckDB extension {name!r} is not installed for this engine (extension dir "
            f"{EXTENSION_DIR or 'default'}). The runtime never INSTALLs at cold start; bake it "
            "with `python scripts/install_extensions.py` (the Dockerfile does)."
        ) from e


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


def similar_names(
    sql_error: str, visible: frozenset[str] | None = None, denied: dict | None = None
) -> str:
    """Zero-network hint appended AFTER a verbatim engine error.

    Invariant 5 keeps the engine's own message first and unchanged — the model
    repairs from it. DuckDB already names candidates for a column of a table in
    the FROM clause ("Candidate bindings") and for a mistyped table name ("Did
    you mean"). What it cannot know is that the column exists on a DIFFERENT
    table the session can see: `SELECT user_agent FROM cur_line_items` gets
    candidates from cur_line_items only, and the model then guesses. This adds
    that one fact from the engine's own catalog (`duckdb_columns()`), filtered
    to what the principal may see: never a table outside `visible` and
    never a CLS-denied column name.
    """
    m = re.search(r'Referenced column "([^"]+)" not found', sql_error)
    if m is None:
        m = re.search(r"Table with name ([A-Za-z_][A-Za-z0-9_]*) does not exist", sql_error)
        return _table_hint(m.group(1), visible) if m else ""
    return _column_hint(m.group(1), visible, denied)


def _catalog_rows() -> list[tuple[str, str]]:
    """(table, column) for every user table/view the engine can see."""
    with conn_lock:
        return [
            (r[0], r[1])
            for r in get_conn()
            .execute(
                "SELECT table_name, column_name FROM duckdb_columns() "
                "WHERE NOT internal AND schema_name NOT IN ('information_schema', 'pg_catalog')"
            )
            .fetchall()
        ]


def _allowed(table: str, visible: frozenset[str] | None) -> bool:
    return visible is None or table.lower() in visible


def _column_hint(name: str, visible, denied) -> str:
    """ "`user_agent` exists on: cloudtrail_events" — or a near-miss spelling."""
    try:
        rows = _catalog_rows()
    except Exception:  # noqa: BLE001 — a hint must never break the turn
        return ""
    low = name.lower()
    hidden = {t: {c.lower() for c in cols} for t, cols in (denied or {}).items()}
    exact, near = [], {}
    for table, column in rows:
        if not _allowed(table, visible) or column.lower() in hidden.get(table.lower(), set()):
            continue
        if column.lower() == low:
            exact.append(table)
        elif _close(low, column.lower()):
            near.setdefault(column, set()).add(table)
    if exact:
        return f"Hint: column `{name}` exists on: {', '.join(sorted(set(exact))[:5])}."
    if near:
        parts = [f"`{c}` on {', '.join(sorted(ts)[:3])}" for c, ts in sorted(near.items())[:3]]
        return f"Hint: no `{name}` in any table you can query; closest: {'; '.join(parts)}."
    return ""


def _table_hint(name: str, visible) -> str:
    try:
        rows = _catalog_rows()
    except Exception:  # noqa: BLE001
        return ""
    tables = sorted({t for t, _ in rows if _allowed(t, visible)})
    near = [t for t in tables if _close(name.lower(), t.lower())]
    if near:
        return f"Hint: closest table(s) you can query: {', '.join(near[:5])}."
    return f"Hint: tables you can query: {', '.join(tables[:8])}." if tables else ""


def _close(a: str, b: str) -> bool:
    """Near-miss spelling: a high similarity ratio, or one name contained in the
    other and long enough to be meaningful (a bare `t` is inside almost every
    identifier — containment alone made every table a "closest match")."""
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    if short in long and len(short) >= 4 and len(short) / len(long) >= 0.6:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.86


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
        rows = (
            get_conn()
            .execute(
                "SELECT table_name FROM duckdb_tables() WHERE temporary "
                "UNION ALL SELECT view_name FROM duckdb_views() WHERE temporary"
            )
            .fetchall()
        )
        return {r[0] for r in rows}


def reset() -> None:
    """Forget the connection (tests, the eval runner): the next get_conn()
    starts a fresh engine with no temp tables."""
    global _conn
    _conn = None
    VIEW_ERRORS.clear()
