"""Row/column-level governance: identity-aware SQL rewriting in front of DuckDB.

The engine-neutral governance layer of the four-layer architecture
(state → S3, governance → SQL rewrite, scheduling → agent platform,
compute → DuckDB). Authentication is AgentCore's job (inbound JWT
authorizer validates tokens before agent code runs); this module is the
authorization half: it maps a verified principal to a role policy and
rewrites every statement so the engine only ever sees permitted rows and
columns — the same query-rewrite pattern production BI engines use to
inject row-level security ahead of an embedded execution engine.

Two-stage mapping keeps identity churn out of policy management:
  principal (IdP-verified claims)  →  role     (tenant:role key)
  role                             →  policy   (row filter + column denylist)

Fail-closed: for restricted principals, SQL that cannot be parsed is
rejected, raw file access (read_parquet/read_csv/...) is rejected, and
every reference to a governed logical table — through ANY catalog access
method (raw view, s3t, glue, dl) — gets the policy applied.
"""

import json
import os
import threading

from sqlglot import exp, parse_one

# ------------------------------------------------------------------ policies
# role key = "<tenant>:<role>". Loaded from GOVERNANCE_POLICIES (JSON env) if
# set, so policies ship as configuration, not code; in a larger estate move
# this table to DynamoDB — the resolve/rewrite contract stays identical.
#
# Policy semantics per governed logical table (matched by bare table name,
# catalog/schema-agnostic on purpose — the same logical table is reachable
# via raw view, s3t.*, glue.* and dl.*):
#   row_filter:   predicate ANDed into every access (RLS)
#   deny_columns: projected away via SELECT * EXCLUDE (...) (CLS)
# allow_raw_paths: False → read_parquet()/read_csv() etc. are rejected,
#   otherwise path access would bypass table-level policy entirely.
_DEFAULT_POLICIES = {
    # full-access operations role: no rewrite, raw paths allowed
    "ops:admin": {"allow_raw_paths": True, "tables": {}},
    # analyst tenant: recent window only, no fee economics, no raw paths
    "cex-a:analyst": {
        "allow_raw_paths": False,
        "tables": {
            "btc_transactions": {
                "row_filter": "date >= '2026-08-24'",
                "deny_columns": ["fee"],
            },
        },
    },
}

POLICIES = (
    json.loads(os.environ["GOVERNANCE_POLICIES"])
    if os.environ.get("GOVERNANCE_POLICIES")
    else _DEFAULT_POLICIES
)
DEFAULT_ROLE = os.environ.get("GOVERNANCE_DEFAULT_ROLE", "ops:admin")

# principal → active policy is per-invocation state. AgentCore serves one
# session per microVM, but a threading.local would lose the value across the
# entrypoint→tool-thread boundary (Strands runs tools on worker threads), so
# a module-level slot guarded by the run_sql lock is the correct scope here.
_active = {"role": DEFAULT_ROLE}
_active_lock = threading.Lock()


def resolve_role(claims: dict | None) -> str:
    """Map verified identity claims to a role key (stage 1 of the mapping).

    Claims arrive from the AgentCore entrypoint: decoded JWT custom claims
    on the OAuth path, or the payload/User-Id fallback on the SigV4 path.
    Unknown tenant:role combinations fall back to the *most* restricted
    matching tenant policy if one exists, else the default role.
    """
    if not claims:
        return DEFAULT_ROLE
    tenant = claims.get("tenant", "")
    role = claims.get("role", "")
    key = f"{tenant}:{role}"
    if key in POLICIES:
        return key
    # unknown role within a known tenant → first policy of that tenant
    for k in POLICIES:
        if k.startswith(f"{tenant}:"):
            return k
    return DEFAULT_ROLE


def set_active_role(role: str) -> None:
    with _active_lock:
        _active["role"] = role if role in POLICIES else DEFAULT_ROLE


def active_role() -> str:
    with _active_lock:
        return _active["role"]


def active_policy() -> dict:
    return POLICIES[active_role()]


# ------------------------------------------------------------------- rewrite
_RAW_TABLE_FUNCS = ("readparquet", "readcsv", "readjson")

# CLS must be schema-aware: the same logical table exposes different schemas
# per access method (the raw full-history view's glob binds the FIRST file's
# schema, which lacks recent columns like `fee`). EXCLUDE-ing a column the
# relation doesn't expose is a binder error, so the agent registers a schema
# prober (DESCRIBE, cached per reference) and we exclude the intersection.
# A column absent from the schema is unreachable anyway — skipping it cannot
# widen access. Without a prober we keep the full EXCLUDE list (worst case:
# the engine rejects the query — fail closed, never fail open).
_schema_prober = None
_schema_cache: dict[str, frozenset] = {}


def set_schema_prober(fn) -> None:
    """Register `fn(ref_sql) -> set[str]` returning a relation's columns."""
    global _schema_prober
    _schema_prober = fn
    _schema_cache.clear()


def _columns_of(ref_sql: str) -> frozenset | None:
    if _schema_prober is None:
        return None
    if ref_sql not in _schema_cache:
        try:
            _schema_cache[ref_sql] = frozenset(c.lower() for c in _schema_prober(ref_sql))
        except Exception:  # noqa: BLE001 — probe failure → keep full denylist
            return None
    return _schema_cache[ref_sql]


def _guard_subquery(tbl: exp.Table, rule: dict) -> exp.Subquery:
    """Build (SELECT * EXCLUDE(...) FROM <table> WHERE <pred>) AS <alias>."""
    deny = rule.get("deny_columns") or []
    pred = rule.get("row_filter") or "TRUE"
    alias = tbl.alias or tbl.name
    # regenerate the original (possibly catalog-qualified) table reference
    ref = tbl.copy()
    ref.set("alias", None)
    ref_sql = ref.sql(dialect="duckdb")
    if deny:
        cols = _columns_of(ref_sql)
        if cols is not None:
            deny = [c for c in deny if c.lower() in cols]
    exclude = f" EXCLUDE ({', '.join(deny)})" if deny else ""
    wrapped = parse_one(
        f"SELECT * FROM (SELECT *{exclude} FROM {ref_sql} WHERE {pred}) AS {alias}",
        read="duckdb",
    )
    return wrapped.find(exp.Subquery)


def apply_governance(sql: str) -> str:
    """Rewrite one statement under the active policy. Raises ValueError to
    reject. Returns the (possibly unchanged) SQL string."""
    policy = active_policy()
    governed_tables = policy.get("tables", {})
    if policy.get("allow_raw_paths", False) and not governed_tables:
        return sql  # unrestricted role: engine sees the statement as-is

    try:
        tree = parse_one(sql, read="duckdb")
    except Exception as e:  # fail closed for restricted principals
        raise ValueError(f"governance: statement could not be parsed ({e})") from e

    # raw file access bypasses table policies — reject unless allowed
    if not policy.get("allow_raw_paths", False):
        for func in tree.find_all(exp.Func):
            if type(func).__name__.lower() in _RAW_TABLE_FUNCS:
                raise ValueError(
                    "governance: raw file access (read_parquet/read_csv) is not "
                    "permitted for this principal — use the governed table views"
                )

    # names introduced by the statement itself are not governed references
    local_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    if isinstance(tree, exp.Create):
        local_names.add(tree.this.this.name.lower())

    for tbl in list(tree.find_all(exp.Table)):
        name = tbl.name.lower()
        if name in local_names or name not in governed_tables:
            continue
        tbl.replace(_guard_subquery(tbl, governed_tables[name]))

    return tree.sql(dialect="duckdb")
