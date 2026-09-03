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
rejected, statement shapes sqlglot cannot model (EXPLAIN, opaque commands)
are rejected, raw file access (read_parquet/read_csv/...) is rejected, no
local name (CTE, temp table/view, alias) may reuse a governed table's name,
and every reference to a governed logical table — through ANY catalog access
method (raw view, s3t, glue, dl) — gets the policy applied. A principal
without a policy resolves to the built-in deny-all role.
"""

import copy
import json
import os
import re
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
# deny_all: True → every statement is rejected (the floor for principals
#   that carry no usable identity; see DENY_ALL_ROLE).
# The scenario has three principals:
#   ops:admin              — platform operations, unrestricted, raw paths allowed
#   exchange-a:investigator — case worker at exchange A: sees only the case's
#     date window on the ledger, and only its OWN exchange's deposit labels
#   exchange-a:junior      — junior analyst: same row scope, but the deposit
#     labels' subject_identity (customer PII linkage) is column-denied
_DEFAULT_POLICIES = {
    # full-access operations role: no rewrite, raw paths allowed
    "ops:admin": {"allow_raw_paths": True, "tables": {}},
    "exchange-a:investigator": {
        "allow_raw_paths": False,
        "tables": {
            # ledger: only the open-case incident window
            "eth_traces": {"row_filter": "date >= '2025-02-21'"},
            # attribution: this tenant sees only its own exchange's labels
            "cex_deposit_labels": {"row_filter": "exchange = 'exchange-a'"},
        },
    },
    "exchange-a:junior": {
        "allow_raw_paths": False,
        "tables": {
            "eth_traces": {"row_filter": "date >= '2025-02-21'"},
            "cex_deposit_labels": {
                "row_filter": "exchange = 'exchange-a'",
                "deny_columns": ["subject_identity"],
            },
        },
    },
}

# The floor. Missing/blank/unknown-tenant claims land here, and so does any
# deployment that forgets GOVERNANCE_DEFAULT_ROLE. It is always present in the
# policy table (an operator-supplied table cannot remove it) and rejects every
# statement before parsing. The previous default was ops:admin — an
# unauthenticated-looking request became the unrestricted principal, which
# the Fail-Safe NFR forbids.
DENY_ALL_ROLE = "deny-all"
_DENY_ALL_POLICY = {"allow_raw_paths": False, "deny_all": True, "tables": {}}

_ROLE_KEY_RE = re.compile(r"^[^:\s]+:[^:\s]+$")  # "<tenant>:<role>", no whitespace
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # bare SQL identifier
_POLICY_KEYS = {"allow_raw_paths", "tables", "deny_all"}
_RULE_KEYS = {"row_filter", "deny_columns"}


def _validate_policies(policies: dict) -> None:
    """Fail-stop on a malformed policy table (T011/M013).

    Everything here is interpolated into SQL or compared against parsed
    identifiers, so shape errors would otherwise surface as silent
    over-exposure (a typo'd table name governs nothing) or as SQL injection
    from configuration. Rejecting at import means a bad table never serves a
    single query.
    """
    if not isinstance(policies, dict) or not policies:
        raise ValueError("governance: GOVERNANCE_POLICIES must be a non-empty JSON object")
    for key, policy in policies.items():
        where = f"policy {key!r}"
        if key != DENY_ALL_ROLE and not _ROLE_KEY_RE.match(key):
            raise ValueError(f"governance: {where}: role key must look like '<tenant>:<role>'")
        if not isinstance(policy, dict):
            raise ValueError(f"governance: {where}: must be an object")
        unknown = set(policy) - _POLICY_KEYS
        if unknown:
            raise ValueError(f"governance: {where}: unknown keys {sorted(unknown)}")
        for flag in ("allow_raw_paths", "deny_all"):
            if flag in policy and not isinstance(policy[flag], bool):
                raise ValueError(f"governance: {where}: {flag} must be a boolean")
        tables = policy.get("tables", {})
        if not isinstance(tables, dict):
            raise ValueError(f"governance: {where}: tables must be an object")
        if policy.get("allow_raw_paths") and tables:
            # a raw path read bypasses every table rule, so this combination
            # promises governance it cannot deliver
            raise ValueError(
                f"governance: {where}: allow_raw_paths=true cannot be combined with governed tables"
            )
        for tname, rule in tables.items():
            twhere = f"{where}, table {tname!r}"
            if not _IDENT_RE.match(tname):
                raise ValueError(f"governance: {twhere}: table name must be a bare identifier")
            if not isinstance(rule, dict) or set(rule) - _RULE_KEYS:
                raise ValueError(f"governance: {twhere}: rule keys must be row_filter/deny_columns")
            pred = rule.get("row_filter")
            if pred is not None:
                if not isinstance(pred, str) or not pred.strip():
                    raise ValueError(f"governance: {twhere}: row_filter must be a non-empty string")
                try:  # must be a single predicate, nothing more
                    probe = parse_one(f"SELECT * FROM t WHERE {pred}", read="duckdb")  # nosec B608 - validation-only parse, never executed
                except Exception as e:  # noqa: BLE001 — any parse failure is a bad policy
                    raise ValueError(
                        f"governance: {twhere}: row_filter does not parse ({e})"
                    ) from e
                if not isinstance(probe, exp.Select) or probe.args.get("where") is None:
                    raise ValueError(f"governance: {twhere}: row_filter must be a WHERE predicate")
            cols = rule.get("deny_columns")
            if cols is not None and (
                not isinstance(cols, list)
                or not all(isinstance(c, str) and _IDENT_RE.match(c) for c in cols)
            ):
                raise ValueError(f"governance: {twhere}: deny_columns must be bare identifiers")
    if not policies[DENY_ALL_ROLE].get("deny_all"):
        raise ValueError(f"governance: policy {DENY_ALL_ROLE!r} must keep deny_all=true")


def _load_policies() -> dict:
    raw = os.environ.get("GOVERNANCE_POLICIES")
    policies = json.loads(raw) if raw else copy.deepcopy(_DEFAULT_POLICIES)
    if isinstance(policies, dict):
        policies.setdefault(DENY_ALL_ROLE, copy.deepcopy(_DENY_ALL_POLICY))
        # table names are matched lowercase against parsed identifiers
        for policy in policies.values():
            if isinstance(policy, dict) and isinstance(policy.get("tables"), dict):
                policy["tables"] = {k.lower(): v for k, v in policy["tables"].items()}
    _validate_policies(policies)
    return policies


POLICIES = _load_policies()
DEFAULT_ROLE = os.environ.get("GOVERNANCE_DEFAULT_ROLE", DENY_ALL_ROLE)
if DEFAULT_ROLE not in POLICIES:  # fail-stop: a typo here must not become "no policy"
    raise ValueError(f"governance: GOVERNANCE_DEFAULT_ROLE {DEFAULT_ROLE!r} is not a policy key")

# principal → active policy is per-invocation state. AgentCore serves one
# session per microVM, but a threading.local would lose the value across the
# entrypoint→tool-thread boundary (Strands runs tools on worker threads), so
# a module-level slot guarded by the run_sql lock is the correct scope here.
_active = {"role": DEFAULT_ROLE}
_active_lock = threading.Lock()


def _restrictiveness(role: str) -> tuple:
    """Sort key for "how restricted is this role" — larger is more restricted.

    Derived from the policy content itself so there is no extra metadata to
    keep in sync. deny_all outranks everything, then denying raw file access
    (a path read bypasses the table policies entirely, so it is the widest
    hole), then the number of governed tables, then row filters, then denied
    columns. Callers sort the role keys first, so ties resolve
    deterministically and the answer never depends on declaration order.
    """
    policy = POLICIES[role]
    tables = policy.get("tables", {}) or {}
    filters = sum(1 for rule in tables.values() if rule.get("row_filter"))
    denies = sum(len(rule.get("deny_columns") or []) for rule in tables.values())
    return (
        1 if policy.get("deny_all", False) else 0,
        0 if policy.get("allow_raw_paths", False) else 1,
        len(tables),
        filters,
        denies,
    )


def resolve_role(claims: dict | None) -> str:
    """Map verified identity claims to a role key (stage 1 of the mapping).

    Claims arrive from the AgentCore entrypoint: decoded JWT custom claims
    on the OAuth path, or the payload/User-Id fallback on the SigV4 path.
    Unknown tenant:role combinations fall back to the *most* restricted
    matching tenant policy if one exists, else the default role (deny-all
    unless the operator configured otherwise).
    """
    if not claims:
        return DEFAULT_ROLE
    tenant = claims.get("tenant", "")
    role = claims.get("role", "")
    key = f"{tenant}:{role}"
    if key in POLICIES:
        return key
    # Unknown role inside a known tenant → that tenant's MOST restricted policy.
    # Returning "the first key that matches the tenant" depended on dict
    # insertion order: a tenant declaring its admin policy first handed full
    # access to every unknown role. No test with one policy per tenant (the
    # built-in table) can observe that, which is why it survived.
    same_tenant = [k for k in POLICIES if k.startswith(f"{tenant}:")]
    if same_tenant:
        return max(sorted(same_tenant), key=_restrictiveness)
    return DEFAULT_ROLE


def set_active_role(role: str) -> None:
    with _active_lock:
        _active["role"] = role if role in POLICIES else DEFAULT_ROLE


def active_role() -> str:
    with _active_lock:
        return _active["role"]


def active_policy() -> dict:
    return POLICIES[active_role()]


def is_denied_all() -> bool:
    """True when the active principal has no usable policy (deny-all floor)."""
    return bool(active_policy().get("deny_all", False))


def describe_applied(sql: str) -> dict:
    """Summarize what the active policy applies to the governed tables named in
    `sql` — for the UI trace, so a reader sees WHICH rows/columns were governed,
    not just that governance ran. Best-effort: parse failures return empty."""
    policy = active_policy()
    tables = policy.get("tables", {})
    if policy.get("allow_raw_paths", False) and not tables:
        return {"rls": [], "cls": []}
    try:
        tree = parse_one(sql, read="duckdb")
    except Exception:  # noqa: BLE001 — trace is best-effort; rewrite path owns fail-closed
        return {"rls": [], "cls": []}
    referenced = {t.name.lower() for t in tree.find_all(exp.Table)}
    rls, cls = [], []
    for name, rule in tables.items():
        if name not in referenced:
            continue
        if rule.get("row_filter"):
            rls.append({"table": name, "predicate": rule["row_filter"]})
        for col in rule.get("deny_columns") or []:
            cls.append({"table": name, "column": col})
    return {"rls": rls, "cls": cls}


# ------------------------------------------------------------------- rewrite
# Raw file access must be rejected for restricted principals, otherwise a path
# read bypasses every table-level policy. This is an ALLOWLIST on purpose.
#
# The previous denylist matched sqlglot's Python CLASS name against
# ("readparquet", "readcsv", "readjson"). sqlglot only models a handful of
# functions as dedicated classes; everything else parses to `exp.Anonymous`,
# whose class name is always "anonymous". Measured against DuckDB's function
# catalogue, 7 of 10 file-reading functions slipped through — including
# `parquet_scan` (a direct alias of read_parquet) and `read_csv_auto` — and
# "readjson" could never match at all because sqlglot has no ReadJSON class.
# Enumerating names is not a fix either: DuckDB adds table functions every
# release, so a denylist is permanently behind.
#
# The shape of the AST gives a stable test instead: every FROM/JOIN source is an
# `exp.Table`, and a plain table reference — bare, catalog-qualified, CTE name
# or temp table — always has an `exp.Identifier` as its `this`. A table-valued
# function never does. So: only Identifier sources are allowed.
#
# Exception list, by AST class, for table functions that provably cannot reach
# external storage (pure generators). Keep it minimal and justify additions:
#   GenerateSeries — range() / generate_series(), arithmetic only
# (`FROM unnest([...])` needs no entry: it produces no exp.Table node at all.)
_TABLE_FUNC_ALLOWED = ("GenerateSeries",)

# Statement shapes the rewrite can reason about. Same posture as the table
# function check — an allowlist on the AST, because the failure mode of a
# denylist is silent: sqlglot parses anything it does not model (EXPLAIN,
# `SHOW <table>`, future syntax) into an opaque `exp.Command` that contains
# NO exp.Table nodes, so the rewrite loop found nothing to guard and returned
# the statement untouched. `EXPLAIN ANALYZE SELECT ... FROM cex_deposit_labels`
# then executed the ungoverned query and returned real cardinalities of
# denied rows in the plan — a fail-open the read-only gate lets through
# because "explain" is a read keyword. Anything outside this tuple is rejected
# for restricted principals; the unrestricted role never reaches this check.
_GOVERNABLE_ROOTS = (
    exp.Select,
    exp.SetOperation,  # UNION / INTERSECT / EXCEPT
    exp.Subquery,  # a parenthesised query
    exp.Create,  # CREATE TEMP TABLE|VIEW ... AS <query> (the gate enforces TEMP)
    exp.Describe,
    exp.Summarize,
    exp.Show,  # SHOW TABLES etc.; `SHOW <table>` parses as Command and is rejected
)

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
    """Build (SELECT * EXCLUDE(...) FROM <table> WHERE <pred>) AS <alias>.

    Query modifiers hanging off the table node (PIVOT) are moved OUT of the
    guard onto the returned subquery, so the policy is applied to the base
    relation and the modifier operates on governed rows — never the reverse.
    """
    deny = rule.get("deny_columns") or []
    pred = rule.get("row_filter") or "TRUE"
    alias = tbl.alias or tbl.name
    # regenerate the original (possibly catalog-qualified) table reference
    ref = tbl.copy()
    ref.set("alias", None)
    pivots = ref.args.pop("pivots", None)
    ref_sql = ref.sql(dialect="duckdb")
    if deny:
        cols = _columns_of(ref_sql)
        if cols is not None:
            deny = [c for c in deny if c.lower() in cols]
    exclude = f" EXCLUDE ({', '.join(deny)})" if deny else ""
    # Injection note: `alias` comes from the model (Table.alias, a raw string)
    # and `ref_sql`/`pred` from sqlglot and the policy respectively. This string
    # is never executed — it is parsed, and only the exp.Subquery node is
    # extracted and returned, so anything an alias appends after the subquery is
    # discarded rather than emitted. Verified: an alias of
    # `x; DROP TABLE victim` yields `... ) AS x` and the tail never reaches the
    # engine. That safety rests on taking find(exp.Subquery) from the parse
    # result — if this ever returns the whole tree instead, quote `alias` first.
    wrapped = parse_one(
        f"SELECT * FROM (SELECT *{exclude} FROM {ref_sql} WHERE {pred}) AS {alias}",  # nosec B608 - see injection note above
        read="duckdb",
    )
    guard = wrapped.find(exp.Subquery)
    if pivots:
        guard.set("pivots", pivots)
    return guard


def _local_names(tree: exp.Expression) -> set[str]:
    """Every name the statement introduces itself: CTE names, the CREATE
    target, derived-table aliases, and table aliases that differ from the
    table's own name."""
    names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    if isinstance(tree, exp.Create):
        names.add(tree.this.name.lower())
    names.update(sq.alias.lower() for sq in tree.find_all(exp.Subquery) if sq.alias)
    names.update(t.alias.lower() for t in tree.find_all(exp.Table) if t.alias and t.alias != t.name)
    return names


def apply_governance(sql: str) -> str:
    """Rewrite one statement under the active policy. Raises ValueError to
    reject. Returns the (possibly unchanged) SQL string."""
    policy = active_policy()
    if policy.get("deny_all", False):
        raise ValueError(
            f"governance: no access policy is assigned to principal '{active_role()}' — "
            "access denied"
        )
    governed_tables = policy.get("tables", {})
    if policy.get("allow_raw_paths", False) and not governed_tables:
        return sql  # unrestricted role: engine sees the statement as-is

    try:
        tree = parse_one(sql, read="duckdb")
    except Exception as e:  # fail closed for restricted principals
        raise ValueError(f"governance: statement could not be parsed ({e})") from e

    # statement shape must be one the rewrite can see through (see the note
    # on _GOVERNABLE_ROOTS: an opaque Command carries no table nodes)
    if isinstance(tree, exp.Command) or not isinstance(tree, _GOVERNABLE_ROOTS):
        shown = tree.this if isinstance(tree, exp.Command) else type(tree).__name__
        raise ValueError(
            f"governance: statement type {str(shown).strip()!r} cannot be governed for this "
            "principal — use SELECT / WITH / DESCRIBE / SUMMARIZE or CREATE TEMP TABLE ... AS"
        )

    # raw file access bypasses table policies — reject unless allowed
    if not policy.get("allow_raw_paths", False):
        for tbl in tree.find_all(exp.Table):
            inner = tbl.this
            if isinstance(inner, exp.Identifier):
                continue  # a real table/view/CTE reference
            kind = type(inner).__name__
            if kind in _TABLE_FUNC_ALLOWED:
                continue
            # Anonymous carries the real function name in `this`; modelled
            # functions (ReadParquet, ...) only have the class name.
            shown = inner.this if kind == "Anonymous" else kind
            raise ValueError(
                f"governance: raw file access via table function {shown!r} is not "
                "permitted for this principal — use the governed tables"
            )

    # No local name may reuse a governed table's name. The previous design
    # collected CTE/CTAS names and SKIPPED any table node bearing one — so
    # `WITH cex_deposit_labels AS (SELECT * FROM cex_deposit_labels) SELECT
    # subject_identity FROM cex_deposit_labels` made the governed name "local",
    # the inner base-table reference was never guarded, and the denied-column
    # check was skipped too: a junior read every tenant's PII (also via
    # `CREATE TEMP TABLE cex_deposit_labels AS SELECT * FROM cex_deposit_labels`,
    # where the CTAS target exempted the source on the right-hand side).
    # Scoping the exemption correctly is fragile (recursive CTEs, nested
    # scopes, DuckDB's temp-schema search order); refusing the collision is
    # not. With no collision, every exp.Table node that carries a governed
    # name IS a reference to the governed table, and every one gets guarded.
    clash = sorted(_local_names(tree) & set(governed_tables))
    if clash:
        raise ValueError(
            f"governance: local name(s) {clash} shadow governed table(s) — a CTE, temp "
            "table/view or alias may not reuse a governed table's name; choose another name"
        )

    # A restricted principal that explicitly names a denied column is a clean
    # AuthZ denial — reject with a message the model can relay ("this column is
    # not visible to your role"), rather than silently EXCLUDE-ing it and
    # letting the engine raise an opaque binder error later. Data is never
    # exposed either way (fail-closed); this just makes the boundary legible.
    _reject_denied_columns(tree, governed_tables)

    for tbl in list(tree.find_all(exp.Table)):
        name = tbl.name.lower()
        if name not in governed_tables:
            continue
        guard = _guard_subquery(tbl, governed_tables[name])
        parent = tbl.parent
        if isinstance(parent, exp.Describe | exp.Summarize) and parent.this is tbl:
            # DESCRIBE <table> / SUMMARIZE <table> take a relation, not a
            # parenthesised subquery: emit DESCRIBE SELECT * FROM (guard)
            tbl.replace(exp.select("*").from_(guard))
        else:
            tbl.replace(guard)

    return tree.sql(dialect="duckdb")


def _reject_denied_columns(tree, governed_tables: dict) -> None:
    """Raise ValueError if the statement explicitly references a column denied
    to the active role. Scans column references and matches denied names of any
    governed table present in the statement. Conservative on purpose: if a bare
    column name matches a denied column of a governed table in scope, reject —
    the alternative (silent EXCLUDE) hides the boundary behind a binder error."""
    # denied columns across all governed tables actually referenced here
    referenced = {t.name.lower() for t in tree.find_all(exp.Table)}
    denied: set[str] = set()
    for name, rule in governed_tables.items():
        if name in referenced:
            denied.update(c.lower() for c in (rule.get("deny_columns") or []))
    if not denied:
        return
    for col in tree.find_all(exp.Column):
        if col.name.lower() in denied:
            raise ValueError(
                f"governance: column '{col.name}' is not visible to role "
                f"'{active_role()}' — your access policy hides it"
            )
