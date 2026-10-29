"""Cost policy as a rewrite.

The second policy species in the pre-engine rewrite layer: governance decides
*who may see what*, this module decides *what a statement may cost*. Both run
before the engine sees the SQL; both are engine-neutral sqlglot rewrites.

Two mechanisms, both validated in production data-agent deployments, where
EXPLAIN estimates proved not to be a trustworthy refusal basis on cloud engines:

1. **Required partition filter.** A reference to a partitioned raw source (the
   hive-partitioned S3 views in `storage.registry.PARTITIONED_SOURCES`, or a
   `read_parquet(...)` over one of their whole-prefix globs) without a
   predicate on its partition column is rejected before execution. Temp
   tables, framework result tables and catalog-leg tables (`s3t.*`, `glue.*`,
   `dl.*` — the catalog prunes by metadata) are exempt.
2. **Path expansion.** A literal, bounded predicate on the partition column
   (`=`, `IN (...)`, `BETWEEN`, `>= ... AND <= ...`) is rewritten into an
   explicit list of per-partition paths, so the engine LISTs N partition
   prefixes instead of the whole table prefix. Bounded by
   `MAX_EXPANDED_PARTITIONS`; unbounded or non-literal predicates satisfy the
   filter requirement but are left for the engine's own pruning.
"""

from __future__ import annotations

import datetime as dt
import os
import re

import sqlglot
from sqlglot import exp

from data_agent.storage.registry import PARTITIONED_SOURCES

MAX_EXPANDED_PARTITIONS = int(os.environ.get("MAX_EXPANDED_PARTITIONS", "366"))

# The DECLARED single-node envelope, below the hard cap. A statement that
# expands past this many partition paths is refused WITH A ROUTE — a structured
# suggestion to run the equivalent governed SQL on a warehouse-scale engine
# over the same lake — instead of being executed toward an OOM. The threshold
# is conservative on purpose (volume per partition is not estimated — that
# would need network); an admin overrides it per deployment. 0 disables.
UPGRADE_ADVICE_PATHS = int(os.environ.get("UPGRADE_ADVICE_PATHS", "120"))

# Session budget. A single statement is bounded by the partition gate and
# the memory envelope; a SESSION is not — an agent loop can spend a tenant's
# quota in statements that are each individually cheap (measured elsewhere:
# agent-driven query volume 10-100x a human's). These caps are per session,
# enforced in the Cost stage (the layer that already decides what a statement
# may cost) and refused with a message that says which budget ran out. 0 = off.
SESSION_MAX_STATEMENTS = int(os.environ.get("SESSION_MAX_STATEMENTS", "0"))
SESSION_MAX_EXPANDED_PATHS = int(os.environ.get("SESSION_MAX_EXPANDED_PATHS", "0"))
SESSION_MAX_ENGINE_MS = float(os.environ.get("SESSION_MAX_ENGINE_MS", "0"))

# view name → spec; tests monkeypatch this
SOURCES: dict[str, dict] = dict(PARTITIONED_SOURCES)

_BOUNDED = (exp.EQ, exp.In, exp.Between, exp.GT, exp.GTE, exp.LT, exp.LTE)
_READERS = ("readparquet", "readcsv", "readjson")  # sqlglot class names (lower)
_READER_NAMES = (
    "read_parquet",
    "parquet_scan",
    "read_csv",
    "read_csv_auto",
    "read_json",
    "read_json_auto",
    "read_ndjson_auto",
)


class CostRejected(ValueError):
    """Statement refused by the cost policy (message is model-actionable)."""


class BudgetExceeded(CostRejected):
    """The SESSION's budget is spent — no statement will run in this
    session again, so the message tells the model to stop and report."""


class UpgradeAdvised(CostRejected):
    """The statement exceeds the declared single-node envelope.

    Not executed; `advice` carries the structured payload for the trace event
    (paths, limit, routes, the equivalent governed SQL). Routing stays OUTSIDE
    the agent — the message tells the model to report the route, never to
    retry the same range."""

    def __init__(self, message: str, advice: dict) -> None:
        super().__init__(message)
        self.advice = advice


def check_budget(counters: dict) -> None:
    """Raise BudgetExceeded when a session cap is reached. `counters` is
    `audit.session_counters()` — statements, expanded_paths, engine_ms so far."""
    caps = (
        ("statements", SESSION_MAX_STATEMENTS, "SESSION_MAX_STATEMENTS", "statements"),
        (
            "expanded_paths",
            SESSION_MAX_EXPANDED_PATHS,
            "SESSION_MAX_EXPANDED_PATHS",
            "expanded partition paths",
        ),
        ("engine_ms", SESSION_MAX_ENGINE_MS, "SESSION_MAX_ENGINE_MS", "engine milliseconds"),
    )
    for key, cap, knob, label in caps:
        if cap and counters.get(key, 0) >= cap:
            raise BudgetExceeded(
                f"session budget: this session has used {counters[key]:g} {label}, the limit is "
                f"{cap:g} ({knob}). No further statement will run in this session — summarize "
                "what you already have and tell the user the budget is exhausted."
            )


def apply(sql: str) -> tuple[str, dict]:
    """Return (possibly rewritten sql, info). Raises CostRejected."""
    info: dict = {"sources": [], "partition_filter": None, "expanded_paths": 0}
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:  # noqa: BLE001 — DuckDB syntax sqlglot cannot model
        return _fallback(sql, info)
    local = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    changed = False
    for tbl in list(tree.find_all(exp.Table)):
        hit = _source_of(tbl, local)
        if hit is None:
            continue
        name, spec = hit
        if name not in info["sources"]:
            info["sources"].append(name)
        column = spec["column"]
        scope = tbl.find_ancestor(exp.Select)
        in_scope, anywhere = _partition_predicates(tree, scope, tbl, column)
        if not in_scope and not anywhere:
            raise CostRejected(
                f"cost policy: scanning `{name}` requires a filter on its partition column "
                f"`{column}` (e.g. WHERE {column} = '<value>' or {column} BETWEEN ... AND ...). "
                "Full-prefix scans are refused before the engine runs; temp tables and "
                "catalog tables are exempt."
            )
        info["partition_filter"] = True
        parts = _literal_partitions(in_scope, spec["granularity"])
        if parts is None:
            # non-literal or one-sided predicate (`date >= x`, `date = (subquery)`):
            # the filter requirement is met and the engine prunes on listing.
            continue
        if not parts:
            # a literal but UNSATISFIABLE range (lower bound above upper) — it
            # would read nothing, and before the fix it fell through to a
            # whole-prefix scan because no paths were expanded (T004).
            raise CostRejected(
                f"cost policy: the filter on `{name}` selects no `{column}` partitions — "
                "the bounds are unsatisfiable (e.g. a lower bound later than the upper). "
                "Fix the range; an empty range must not degrade to a full scan."
            )
        if len(parts) > MAX_EXPANDED_PARTITIONS:
            # a literal range too wide to expand is a whole-prefix scan by another
            # name — the exact cost the gate exists to refuse. Before the fix it
            # recorded the filter as satisfied and then declined to expand (T004).
            raise CostRejected(
                f"cost policy: the `{column}` range on `{name}` spans more than "
                f"{MAX_EXPANDED_PARTITIONS} {spec['granularity']} partitions — that is a "
                "full-prefix scan and is refused before the engine runs. Narrow the range, "
                "use a catalog leg (s3t/glue/dl) which prunes by metadata, or run the "
                "question on a warehouse-scale engine (Amazon Athena / Amazon Redshift)."
            )
        tbl.replace(_expanded_table(tbl, spec, parts))
        info["expanded_paths"] += len(parts)
        changed = True
    if UPGRADE_ADVICE_PATHS and info["expanded_paths"] > UPGRADE_ADVICE_PATHS:
        # Within the hard cap but past the declared envelope — refuse
        # with a route instead of executing toward the memory limit. The
        # equivalent SQL is the GOVERNED statement (logical table names,
        # row/column policy already compiled in), which runs as-is on a
        # warehouse-scale engine over the same lake.
        advice = {
            "expanded_paths": info["expanded_paths"],
            "limit": UPGRADE_ADVICE_PATHS,
            "routes": ["Amazon Athena", "Amazon Redshift"],
            "equivalent_sql": sql,
        }
        raise UpgradeAdvised(
            f"cost policy: this statement expands to {info['expanded_paths']} partition paths "
            f"({', '.join(info['sources'])}) — past this agent's declared single-node envelope "
            f"(UPGRADE_ADVICE_PATHS={UPGRADE_ADVICE_PATHS}). It was NOT executed. Either narrow "
            "the window below the envelope, or tell the user to run the equivalent governed SQL "
            "on a warehouse-scale engine over the same lake (Amazon Athena / Amazon Redshift) — "
            f"routing stays outside this agent:\n{sql}\nDo not retry the same range.",
            advice,
        )
    return (tree.sql(dialect="duckdb") if changed else sql), info


# ------------------------------------------------------------ source match
def _source_of(tbl: exp.Table, local: set[str]) -> tuple[str, dict] | None:
    inner = tbl.this
    if isinstance(inner, exp.Identifier):
        if tbl.db or tbl.catalog:
            return None  # catalog legs prune by metadata
        name = tbl.name.lower()
        if "://" in name or "/" in name:  # DuckDB replacement scan: FROM 's3://.../traces/'
            return _source_by_path(tbl.name)
        if name in local or name not in SOURCES:
            return None
        return name, SOURCES[name]
    # a reader function over one of our whole-prefix globs
    kind = type(inner).__name__.lower()
    fname = inner.name.lower() if kind == "anonymous" else kind
    if kind not in _READERS and fname not in _READER_NAMES:
        return None
    args = inner.args.get("expressions") or []
    if not args or not isinstance(args[0], exp.Literal) or not args[0].is_string:
        return None  # array form = already explicit paths
    return _source_by_path(args[0].this)


def _source_by_path(path: str) -> tuple[str, dict] | None:
    low = path.lower()
    for name, spec in SOURCES.items():
        base = spec["base"].rstrip("/").lower()
        if (low == base or low.startswith(base + "/")) and f"{spec['column']}=" not in low:
            return name, spec  # whole-prefix reference, not partition-scoped
    return None


# -------------------------------------------------------------- predicates
def _predicate_of(col: exp.Column) -> exp.Expression | None:
    node = col.parent
    while node is not None and not isinstance(node, _BOUNDED):
        if isinstance(node, exp.Select | exp.Where):
            return None
        node = node.parent
    return node


def _transparent(sel: exp.Select) -> bool:
    """`SELECT * [EXCLUDE ...] FROM ... WHERE ...` with no grouping: a predicate
    on the outer side names the same physical column as the inner table."""
    return (
        isinstance(sel, exp.Select)
        and len(sel.expressions) == 1
        and isinstance(sel.expressions[0], exp.Star)
        and not sel.args.get("group")
    )


def _outer_scopes(tree, scope, tbl) -> list[tuple[exp.Select, set[str]]]:
    """One hop outward through a transparent derived table or CTE: the outer
    SELECT whose WHERE also bounds this table, with the qualifiers that name it
    there. This is what lets a governance guard `(SELECT * FROM t WHERE date >=
    a) AS t` compose with the caller's `WHERE date <= b` into one closed range."""
    if scope is None or not _transparent(scope):
        return []
    parent = scope.parent
    out = []
    if isinstance(parent, exp.Subquery):
        outer = parent.find_ancestor(exp.Select)
        if outer is not None:
            out.append((outer, {(parent.alias or "").lower(), tbl.name.lower()}))
    elif isinstance(parent, exp.CTE):
        cte_name = parent.alias_or_name.lower()
        for sel in tree.find_all(exp.Select):
            for src in sel.find_all(exp.Table):
                if src.name.lower() == cte_name and src.find_ancestor(exp.Select) is sel:
                    out.append((sel, {cte_name, (src.alias or "").lower()}))
    return out


def _partition_predicates(tree, scope, tbl, column) -> tuple[list, list]:
    """(predicates that bound THIS table's partitions, predicates anywhere)."""
    in_scope: list = []
    anywhere: list = []
    outer = _outer_scopes(tree, scope, tbl)
    for where in tree.find_all(exp.Where):
        owner = where.parent
        quals: set[str] | None = None
        if scope is not None and owner is scope:
            quals = {"", (tbl.alias or "").lower(), tbl.name.lower()}
        else:
            for sel, names in outer:
                if owner is sel:
                    quals = {""} | names
        for col in where.find_all(exp.Column):
            if col.name.lower() != column:
                continue
            pred = _predicate_of(col)
            if pred is None:
                continue
            q = (col.table or "").lower()
            if q in ("", (tbl.alias or "").lower(), tbl.name.lower()) or (quals and q in quals):
                anywhere.append(pred)
            if quals is not None and q in quals:
                in_scope.append(pred)
    return in_scope, anywhere


def _lit(node) -> str | None:
    return node.this if isinstance(node, exp.Literal) and node.is_string else None


def _literal_partitions(preds: list, granularity: str) -> list[str] | None:
    """Partition values named by literal, bounded predicates; None if any
    predicate is non-literal or unbounded (then the engine prunes on its own)."""
    if not preds:
        return None
    points: set[str] | None = None
    lo = hi = None
    for p in preds:
        if isinstance(p, exp.EQ):
            v = _lit(p.expression) or _lit(p.this)
            if v is None:
                return None
            points = {v} if points is None else points & {v}
        elif isinstance(p, exp.In):
            vals = [_lit(e) for e in p.expressions]
            if not vals or any(v is None for v in vals):
                return None
            points = set(vals) if points is None else points & set(vals)
        elif isinstance(p, exp.Between):
            a, b = _lit(p.args["low"]), _lit(p.args["high"])
            if a is None or b is None:
                return None
            lo, hi = _tighten(lo, a, max), _tighten(hi, b, min)
        elif isinstance(p, exp.GTE | exp.GT | exp.LTE | exp.LT):
            v = _lit(p.expression)
            if v is None or not isinstance(p.this, exp.Column):
                return None
            step = _step(granularity)
            if isinstance(p, exp.GT):
                v = _shift(v, granularity, +1)
            if isinstance(p, exp.LT):
                v = _shift(v, granularity, -1)
            if v is None or step is None:
                return None
            if isinstance(p, exp.GTE | exp.GT):
                lo = _tighten(lo, v, max)
            else:
                hi = _tighten(hi, v, min)
    if points is not None:
        vals = sorted(points)
        if lo is not None:
            vals = [v for v in vals if v >= lo]
        if hi is not None:
            vals = [v for v in vals if v <= hi]
        return vals if all(_parse(v, granularity) for v in vals) else None
    if lo is None or hi is None:
        return None  # unbounded on one side
    return _range(lo, hi, granularity)


def _tighten(cur, new, pick):
    return new if cur is None else pick(cur, new)


def _step(granularity):
    return {"day": 1, "month": 1}.get(granularity)


def _parse(v: str, granularity: str):
    try:
        if granularity == "day":
            return dt.date.fromisoformat(v)
        if granularity == "month" and re.fullmatch(r"\d{4}-\d{2}", v):
            return dt.date(int(v[:4]), int(v[5:7]), 1)
    except ValueError:
        return None
    return None


def _fmt(d: dt.date, granularity: str) -> str:
    return d.isoformat() if granularity == "day" else d.strftime("%Y-%m")


def _shift(v: str, granularity: str, n: int) -> str | None:
    d = _parse(v, granularity)
    if d is None:
        return None
    if granularity == "day":
        return _fmt(d + dt.timedelta(days=n), granularity)
    m = d.month - 1 + n
    return _fmt(dt.date(d.year + m // 12, m % 12 + 1, 1), granularity)


def _range(lo: str, hi: str, granularity: str) -> list[str] | None:
    a, b = _parse(lo, granularity), _parse(hi, granularity)
    if a is None or b is None:
        return None  # non-literal bound: undeterminable, leave to the engine
    if a > b:
        return []  # determinable and empty: an unsatisfiable range (caller rejects)
    out = []
    cur = a
    while cur <= b and len(out) <= MAX_EXPANDED_PARTITIONS:
        out.append(_fmt(cur, granularity))
        cur = _parse(_shift(_fmt(cur, granularity), granularity, +1), granularity)
    return out


# --------------------------------------------------------------- rewrite
def _expanded_table(tbl: exp.Table, spec: dict, parts: list[str]) -> exp.Table:
    alias = tbl.alias or tbl.name or "src"
    paths = ", ".join(
        "'" + spec["layout"].format(base=spec["base"].rstrip("/"), part=p) + "'" for p in parts
    )
    call = f"{spec['reader']}([{paths}]{', ' + spec['args'] if spec.get('args') else ''})"
    new = sqlglot.parse_one(f"SELECT * FROM {call} AS {alias}", read="duckdb").find(  # nosec B608 - framework-built path list
        exp.Table
    )
    pivots = tbl.args.get("pivots")
    if pivots:
        new.set("pivots", pivots)
    return new


def _fallback(sql: str, info: dict) -> tuple[str, dict]:
    """sqlglot could not parse: keyword-level check, no expansion."""
    low = sql.lower()
    for name, spec in SOURCES.items():
        if re.search(rf"\b{re.escape(name)}\b", low) or spec["base"].lower() in low:
            info["sources"].append(name)
            if not re.search(rf"\b{re.escape(spec['column'])}\b", low):
                raise CostRejected(
                    f"cost policy: scanning `{name}` requires a filter on its partition "
                    f"column `{spec['column']}`"
                )
            info["partition_filter"] = True
    return sql, info
