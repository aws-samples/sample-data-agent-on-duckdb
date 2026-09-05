"""RLS/CLS rewrite for restricted principals — fail-closed, total by construction.

Every reference to a governed logical table, through any access method,
becomes `(SELECT * EXCLUDE (...) FROM <table> WHERE <pred>) AS <alias>`.
"""

from __future__ import annotations

from sqlglot import exp, parse_one

from . import hardening, policies

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
# relation doesn't expose is a binder error, so the pipeline registers the
# engine's schema prober (DESCRIBE, cached per reference) and we exclude the
# intersection. A column absent from the schema is unreachable anyway —
# skipping it cannot widen access. Without a prober we keep the full EXCLUDE
# list (worst case: the engine rejects the query — fail closed, never open).
_schema_prober = None
_schema_cache: dict[str, frozenset] = {}


def set_schema_prober(fn) -> None:
    """Register `fn(ref_sql) -> set[str]` returning a relation's columns."""
    global _schema_prober
    _schema_prober = fn
    _schema_cache.clear()


# Table-level deny-by-default: a restricted principal may reference a
# table only if its policy names it, it is local to the statement (CTE / CTAS
# target / alias), or it is a session temp object the principal created. The
# session-object lister is registered by the pipeline (compute.engine), so
# this module still imports nothing from compute. No lister → fail closed:
# the empty set means a bare temp read is refused rather than allowed.
_session_lister = None


def set_session_lister(fn) -> None:
    """Register `fn() -> set[str]` returning the session's temp object names."""
    global _session_lister
    _session_lister = fn


def _session_objects() -> frozenset[str]:
    if _session_lister is None:
        return frozenset()
    try:
        return frozenset(n.lower() for n in _session_lister())
    except Exception:  # noqa: BLE001 — listing failed → no exemptions (fail closed)
        return frozenset()


def _columns_of(ref_sql: str) -> frozenset | None:
    if _schema_prober is None:
        return None
    if ref_sql not in _schema_cache:
        try:
            _schema_cache[ref_sql] = frozenset(c.lower() for c in _schema_prober(ref_sql))
        except Exception:  # noqa: BLE001 — probe failure → keep full denylist
            return None
    return _schema_cache[ref_sql]


def describe_applied(sql: str) -> dict:
    """Summarize what the active policy applies to the governed tables named in
    `sql` — for the UI trace, so a reader sees WHICH rows/columns were governed,
    not just that governance ran. Best-effort: parse failures return empty."""
    policy = policies.active_policy()
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
    # `ref_sql` (sqlglot re-serialization) and `pred`/`deny` (validated policy)
    # are safe to interpolate; `alias` is the one part that arrives from the
    # model as a raw string (Table.alias). It is NOT interpolated into the
    # parsed text — the guard body is parsed with a fixed placeholder alias and
    # the real alias is attached through the AST, quoted when it is not a bare
    # identifier. A hostile alias therefore cannot (a) break the parse — an
    # alias of `a)` used to raise a sqlglot ParseError that escaped the Govern
    # stage's ValueError handler and left the attempt unaudited (T006) — nor
    # (b) append a statement, since it never becomes free SQL text at all.
    wrapped = parse_one(
        f"SELECT * FROM (SELECT *{exclude} FROM {ref_sql} WHERE {pred}) AS _guard",  # nosec B608 - ref_sql/pred are sqlglot/policy-derived; alias attached via AST below
        read="duckdb",
    )
    guard = wrapped.find(exp.Subquery)
    guard.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
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
    policy = policies.active_policy()
    if policy.get("deny_all", False):
        raise ValueError(
            f"governance: no access policy is assigned to principal '{policies.active_role()}' — "
            "access denied"
        )
    tree = hardening.universal_checks(sql)  # every principal, the unrestricted role included
    governed_tables = policy.get("tables", {})
    if policy.get("allow_raw_paths", False) and not governed_tables:
        return sql  # unrestricted role: engine sees the statement as-is

    if tree is None:  # fail closed for restricted principals
        try:
            parse_one(sql, read="duckdb")
        except Exception as e:
            raise ValueError(f"governance: statement could not be parsed ({e})") from e
        raise ValueError("governance: statement could not be parsed")

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
            if hardening.is_path_ref(tbl):
                raise ValueError(
                    f"governance: raw file access via path {tbl.name!r} is not permitted "
                    "for this principal — use the governed tables"
                )
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
    local = _local_names(tree)
    clash = sorted(local & set(governed_tables))
    if clash:
        raise ValueError(
            f"governance: local name(s) {clash} shadow governed table(s) — a CTE, temp "
            "table/view or alias may not reuse a governed table's name; choose another name"
        )

    # Table-level deny-by-default: the rewrite only guards tables the
    # policy NAMES, so a mounted-but-unlisted table (another scenario's, or
    # the runtime's own cw_logs export) would otherwise execute ungoverned —
    # a restricted principal read the full contents with an ordinary question.
    # Refuse every base-table reference that the policy does not name, is not
    # local to the statement, and is not a session temp object the principal
    # created. A policy entry with an empty rule ({}) means "readable, no
    # row/column restriction"; it is allowed here and skipped by the guard
    # loop below. Path refs and table functions were already handled above.
    referenced = {
        t.name.lower() for t in tree.find_all(exp.Table) if isinstance(t.this, exp.Identifier)
    }
    unknown = referenced - set(governed_tables) - local
    stray = sorted(unknown - _session_objects()) if unknown else []
    if stray:
        allowed = sorted(governed_tables) or ["(none)"]
        raise ValueError(
            f"governance: table(s) {stray} are not in the access policy for role "
            f"'{policies.active_role()}' — this role may query: {allowed}"
        )

    # A restricted principal that explicitly names a denied column is a clean
    # AuthZ denial — reject with a message the model can relay ("this column is
    # not visible to your role"), rather than silently EXCLUDE-ing it and
    # letting the engine raise an opaque binder error later. Data is never
    # exposed either way (fail-closed); this just makes the boundary legible.
    _reject_denied_columns(tree, governed_tables)

    for tbl in list(tree.find_all(exp.Table)):
        name = tbl.name.lower()
        rule = governed_tables.get(name)
        if not rule:  # unlisted (refused above) or listed with no restriction ({})
            continue
        guard = _guard_subquery(tbl, rule)
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
                f"'{policies.active_role()}' — your access policy hides it"
            )
