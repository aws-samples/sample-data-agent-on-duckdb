"""Metric compiler and the SQL surface it is called through.

The surface is a call inside ordinary SQL, expanded into a subquery before the
statement reaches governance:

    SELECT * FROM metric('inbound_eth', address => '0x…', time_window => 'last_7d')

Two reasons it is shaped as SQL rather than as a second tool. The pipeline's
extension point is a stage (`docs/design.md`, `STAGES`), and a stage sees a
statement, not a tool call; and a call that expands to a subquery composes
with everything already in the statement — JOIN a metric to a working set,
wrap it in `CREATE TEMP TABLE`, aggregate it further — which a tool returning
rows cannot do.

Order is not a preference. The semantic layer EXPANDS (a name becomes SQL over
base tables) and governance NARROWS (row filter injected, columns excluded).
Expanding after governance would hand the engine SQL no policy had seen, so
the stage sits at index 1: after the read-only gate, before `Govern`.

A consequence worth stating: governance never sees the `metric(...)` node, so
its table-function allowlist needs no exception for it. If expansion fails,
this module raises and the pipeline rejects — it does not pass the statement
on. Governance would refuse an unexpanded call anyway (an anonymous table
function is not an allowed source), which makes that the second line of
defence rather than the first.

Everything interpolated into SQL text here is either registry-defined (table,
measure, dimension and parameter columns, constant filters) or a value
validated to a fixed shape first: dates must match `YYYY-MM-DD` / `YYYY-MM`,
and free-text parameter values are quoted with doubled quotes. The model
chooses which metric and which slice; it never contributes SQL.
"""

from __future__ import annotations

import datetime as dt
import re

from sqlglot import exp, parse_one

from data_agent.storage import registry

from .metrics import ANCHOR, METRICS, windows_for

CALL_NAME = "metric"

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


class MetricNotCovered(Exception):
    """The registry does not define what was asked for.

    Raised for an unknown metric, an unsupported dimension, a window the
    metric's granularity cannot honour, a missing required parameter, or a
    malformed call. Always names what IS available: the model's next attempt
    should be a correct call, not a guess.
    """


# --------------------------------------------------------------- literals


def _literal(value) -> str:
    """A SQL literal for a parameter value. Strings are single-quoted with
    doubled quotes; bools and numbers are rendered bare. Anything else is
    refused rather than coerced."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    if not isinstance(value, str):
        raise MetricNotCovered(
            f"parameter values must be strings or numbers, got {type(value).__name__}"
        )
    return "'" + value.replace("'", "''") + "'"


def _date(value: str, granularity: str, what: str) -> str:
    """A date bound, validated to the shape the granularity uses. Bounds are
    interpolated into the partition predicate, so a bound that is not exactly a
    date is refused here rather than escaped."""
    pattern, shape = (_DAY_RE, "YYYY-MM-DD") if granularity == "day" else (_MONTH_RE, "YYYY-MM")
    if not isinstance(value, str) or not pattern.match(value):
        raise MetricNotCovered(f"{what} must be a {shape} date, got {value!r}")
    return value


def _day_range(anchor: str, days: int) -> tuple[str, str]:
    end = dt.date.fromisoformat(anchor)
    return (end - dt.timedelta(days=days - 1)).isoformat(), end.isoformat()


def _month_range(anchor: str, months: int) -> tuple[str, str]:
    year, month = (int(p) for p in anchor.split("-"))
    total = year * 12 + (month - 1) - (months - 1)
    return f"{total // 12:04d}-{total % 12 + 1:02d}", anchor


# ------------------------------------------------------------- compilation


def _time_predicate(name: str, m: dict, time_window, start, end) -> str | None:
    col, gran = m["time_column"], m["granularity"]
    allowed = windows_for(gran)
    if start or end:
        if time_window:
            raise MetricNotCovered(
                f"metric {name!r}: pass either time_window or start_date/end_date, not both"
            )
        if not (start and end):
            raise MetricNotCovered(
                f"metric {name!r}: an explicit window needs both start_date and end_date"
            )
        lo = _date(start, gran, "start_date")
        hi = _date(end, gran, "end_date")
        return f"{col} BETWEEN '{lo}' AND '{hi}'"
    if time_window:
        if time_window not in allowed:
            raise MetricNotCovered(
                f"metric {name!r} is partitioned by {gran}, so it does not accept the window "
                f"{time_window!r}. It accepts {sorted(allowed)}, or an explicit "
                "start_date/end_date."
            )
        anchor = m.get("anchor") or ANCHOR
        if not anchor:
            raise MetricNotCovered(
                f"metric {name!r} has no anchor date for a relative window (its dataset's end "
                "date is deployment-specific). Pass start_date/end_date explicitly, or set "
                "SEMANTIC_ANCHOR."
            )
        if gran == "month" and _DAY_RE.match(anchor):
            anchor = anchor[:7]  # a day anchor names the month that contains it
        anchor = _date(anchor, gran, "anchor")
        lo, hi = (_day_range if gran == "day" else _month_range)(anchor, allowed[time_window])
        return f"{col} BETWEEN '{lo}' AND '{hi}'"
    if m["table"] in registry.PARTITIONED_SOURCES:
        # The cost gate rejects a partitioned source scanned without a predicate
        # on its partition column. Refusing here says which windows exist; the
        # gate's message can only say that one is missing.
        raise MetricNotCovered(
            f"metric {name!r} reads the partitioned source {m['table']!r}, which may not be "
            f"scanned without a window. Pass time_window => one of {sorted(allowed)}, "
            "or start_date/end_date."
        )
    return None


def compile_metric(
    name: str,
    *,
    time_window: str | None = None,
    start: str | None = None,
    end: str | None = None,
    group_by=(),
    filters: dict | None = None,
    params: dict | None = None,
) -> dict:
    """Compile one metric call to SQL. Raises MetricNotCovered for anything the
    registry does not define — never a plausible substitute."""
    if name not in METRICS:
        raise MetricNotCovered(
            f"metric {name!r} is not defined. Defined metrics: {sorted(METRICS)}"
        )
    m = METRICS[name]
    group_by = list(group_by or ())
    filters = dict(filters or {})
    params = dict(params or {})
    dims = m["dimensions"]

    for d in list(group_by) + list(filters):
        if d not in dims:
            raise MetricNotCovered(
                f"metric {name!r} cannot be sliced by {d!r}. It supports {sorted(dims)}."
            )

    where = list(m.get("constant_filters") or ())
    spec = m.get("params") or {}
    unknown = sorted(set(params) - set(spec))
    if unknown:
        raise MetricNotCovered(
            f"metric {name!r} takes no parameter {unknown[0]!r}. It takes "
            f"{sorted(spec) or 'no parameters'}; dimensions go in group_by or filters."
        )
    for pname, pspec in spec.items():
        if pname not in params:
            if pspec.get("required"):
                raise MetricNotCovered(f"metric {name!r} requires the {pname!r} parameter")
            continue
        col, lit = pspec["column"], _literal(params[pname])
        where.append(f"lower({col}) = lower({lit})" if pspec.get("lower") else f"{col} = {lit}")

    pred = _time_predicate(name, m, time_window, start, end)
    if pred:
        where.append(pred)
    for d, v in filters.items():
        where.append(f"{dims[d]} = {_literal(v)}")

    if m.get("type") == "ratio":
        scale = " * 100" if m.get("as_pct") else ""
        measure = f"CAST({m['numerator']} AS DOUBLE){scale} / NULLIF({m['denominator']}, 0)"
    else:
        measure = m["measure"]

    select = [f"{dims[d]} AS {d}" for d in group_by] + [f"{measure} AS {name}"]
    sql = f"SELECT {', '.join(select)} FROM {m['table']}"  # nosec B608 - registry-defined identifiers
    for join in m.get("joins") or ():
        sql += f" {join}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group_by:
        cols = ", ".join(dims[d] for d in group_by)
        sql += f" GROUP BY {cols} ORDER BY {cols}"

    return {
        "sql": sql,
        "metric": name,
        "label": m["label"],
        "owner": m["owner"],
        "unit": m["unit"],
        "version": m["version"],
        "definition": m["definition"],
        "scope": " AND ".join(where) or "all rows",
        "group_by": group_by,
    }


# ----------------------------------------------------------- the SQL surface


def _string(node, key: str) -> str:
    if isinstance(node, exp.Literal) and node.is_string:
        return node.this
    raise MetricNotCovered(f"{key} must be a quoted string, got {node.sql(dialect='duckdb')!r}")


def _string_list(node, key: str) -> list[str]:
    if isinstance(node, exp.Array):
        return [_string(i, key) for i in node.expressions]
    return [_string(node, key)]  # a single dimension may be written unbracketed


def _string_map(node, key: str) -> dict[str, str]:
    if not isinstance(node, exp.Struct):
        raise MetricNotCovered(
            f"{key} must be a struct, e.g. filters => {{'service': 'AmazonEC2'}}"
        )
    out = {}
    for entry in node.expressions:
        if not isinstance(entry, exp.PropertyEQ):
            raise MetricNotCovered(f"{key} entries must be 'name': value pairs")
        out[entry.this.name] = _string(entry.expression, key)
    return out


def _call_args(fn: exp.Anonymous) -> dict:
    """Read a metric() call's arguments off the AST. Named arguments the
    registry does not know as time_window/start_date/end_date/group_by/filters
    are treated as metric parameters, so a typo surfaces as "takes no parameter
    'adress'" rather than being silently dropped.

    Argument names avoid DuckDB reserved words on purpose: `end => …` and
    `window => …` are syntax errors to DuckDB's own parser, which the read-only
    gate cannot classify, so the statement would reach the engine before anyone
    could say what was wrong with it. Both `=>` (sqlglot Kwarg) and `:=`
    (PropertyEQ) are accepted — DuckDB takes either.
    """
    args = list(fn.expressions)
    if not args or not (isinstance(args[0], exp.Literal) and args[0].is_string):
        raise MetricNotCovered(
            "metric() takes the metric name first, quoted: "
            "metric('inbound_eth', address => '0x…', time_window => 'last_7d')"
        )
    out: dict = {"name": args[0].this}
    params: dict = {}
    for arg in args[1:]:
        if not isinstance(arg, exp.Kwarg | exp.PropertyEQ):
            raise MetricNotCovered(
                "metric() arguments after the name must be named: time_window => 'last_7d'"
            )
        key = arg.this.name.lower()
        if key == "group_by":
            out["group_by"] = _string_list(arg.expression, key)
        elif key == "filters":
            out["filters"] = _string_map(arg.expression, key)
        elif key == "time_window":
            out["time_window"] = _string(arg.expression, key)
        elif key in ("start_date", "end_date"):
            out[key[:-5]] = _string(arg.expression, key)
        else:
            params[key] = _string(arg.expression, key)
    out["params"] = params
    return out


def _is_call(tbl: exp.Table) -> bool:
    fn = tbl.this
    return isinstance(fn, exp.Anonymous) and str(fn.this).lower() == CALL_NAME


def expand(sql: str) -> tuple[str, list[dict]]:
    """Replace every metric() call with its compiled subquery.

    Returns the rewritten SQL and one record per expanded call. SQL with no
    call is returned untouched (and not re-serialized). SQL sqlglot cannot
    parse is also returned untouched: there is nothing to expand, and the
    engine's own error is what the model needs to see.
    """
    if CALL_NAME not in sql.lower():
        return sql, []
    try:
        tree = parse_one(sql, read="duckdb")
    except Exception:  # noqa: BLE001 — nothing to expand; the engine reports the syntax error
        return sql, []
    calls = [t for t in tree.find_all(exp.Table) if _is_call(t)]
    if not calls:
        return sql, []
    resolved = []
    for tbl in calls:
        out = compile_metric(**_call_args(tbl.this))
        alias = tbl.alias or out["metric"]
        # The compiled body is parsed and attached through the AST; the alias is
        # set as an identifier rather than interpolated, the same reason
        # governance/rewrite.py attaches its guard alias that way.
        wrapped = parse_one(f"SELECT * FROM ({out['sql']}) AS _m", read="duckdb")
        guard = wrapped.find(exp.Subquery)
        guard.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
        tbl.replace(guard)
        resolved.append(out)
    return tree.sql(dialect="duckdb"), resolved


def source_footer(resolved: list[dict]) -> str:
    """The definitions behind the numbers, appended to the tool result.

    The trace carries them to the UI, but the model is what writes the answer,
    so the definition has to reach the model: an answer that quotes a figure
    without its scope is the failure this layer exists to prevent.
    """
    lines = []
    for r in resolved:
        lines.append(
            f"metric {r['metric']} {r['version']} — {r['label']}, in {r['unit']}, "
            f"owned by {r['owner']}"
        )
        lines.append(f"  rows in scope: {r['scope']}")
        lines.append(f"  definition: {r['definition']}")
    return "\n".join(lines)


# --------------------------------------------------------- prompt catalogue


def _bare(column: str) -> str:
    return column.rsplit(".", 1)[-1].lower()


def visible_metrics(visible=None, denied=None) -> dict[str, dict]:
    """The metrics the active role can actually run, with denied dimensions
    dropped. Mirrors `context.datamap`: the model is shown a metric only if its
    scenario is mounted and governance would let it read the table, and a
    dimension only if the column is not CLS-denied — otherwise the model calls a
    metric that is then refused, and the refusal reads as a broken tool rather
    than a policy."""
    mounted = {s.name for s in registry.enabled()}
    out: dict[str, dict] = {}
    for name, m in METRICS.items():
        table = m["table"]
        if m["scenario"] not in mounted:
            continue
        if visible is not None and table not in visible:
            continue
        blocked = (denied or {}).get(table, frozenset())
        dims = {d: c for d, c in m["dimensions"].items() if _bare(c) not in blocked}
        out[name] = {**m, "dimensions": dims}
    return out


def catalog_doc(visible=None, denied=None) -> str:
    """The metrics section of the system prompt, filtered to the active role."""
    metrics = visible_metrics(visible, denied)
    if not metrics:
        return ""
    rows = []
    for name, m in sorted(metrics.items()):
        params = ", ".join(
            f"{p}{'' if s.get('required') else ' (optional)'}"
            for p, s in (m["params"] or {}).items()
        )
        rows.append(
            f"| `{name}` | {m['unit']} | {m['table']} | {params or '—'} | "
            f"{', '.join(sorted(m['dimensions'])) or '—'} | "
            f"{', '.join(sorted(windows_for(m['granularity'])))} | {m['owner']} |"
        )
    return (
        "\n## Defined metrics (call these; do not write the aggregate yourself)\n\n"
        "A metric is a number whose definition is frozen in code — which rows count, "
        "which unit, which conversions. Call one inside ordinary SQL and it expands to a "
        "subquery, so it composes (JOIN it, wrap it in CREATE TEMP TABLE, aggregate it "
        "further):\n\n"
        "    SELECT * FROM metric('inbound_eth', address => '0x…', time_window => 'last_7d')\n"
        "    SELECT * FROM metric('unblended_spend', start_date => '2025-01',\n"
        "                         end_date => '2025-02', group_by => ['service'])\n\n"
        "| Metric | Unit | Reads | Parameters | Dimensions | Windows | Owner |\n"
        "|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n\n"
        "Rules: every argument after the metric name is named (`address => '0x…'`). A "
        "window is required for metrics over partitioned sources — use `time_window`, or "
        "`start_date`/`end_date` for an exact range. An unknown metric, dimension or window is "
        "REFUSED with the available names listed; do not work around a refusal by writing "
        "the aggregate by hand, because a hand-written version of a defined metric is a "
        "different number. The definition comes back with the result — cite it when you "
        "report the figure. Numbers with no metric here are yours to write in SQL as usual.\n"
    )
