"""Deterministic metric → SQL compiler (semantic layer v0).

`compile(metric, dims, time_window, filters, limit)` builds ONE DuckDB SELECT
from a registered `Metric` and the caller's parameters. Same parameters, same
SQL, every time — the point of a semantic layer is that the cost basis is not
re-derived per question.

This is a new authorization-adjacent surface: the parameters come from the
model, and the model's parameters come from the user (or from data the user
read). So the compiler is an allowlist on every axis and never
interpolates a parameter as text:

  * `dims` must be dimension NAMES the metric registers; the SQL expression
    comes from the registry, the name only selects it;
  * `filters` may name only the metric's `filters` columns; every value
    becomes a bound `exp.Literal` in the AST (a string literal stays a string
    literal — `' OR 1=1` cannot leave the quotes because there are none to
    leave). A `case_insensitive_filters` column is compared as
    `lower(col) = lower('value')`: the literal is still bound, `lower()` is
    applied by the engine on both sides rather than by us to the value;
  * `time_window` values must match the grain's literal shape (YYYY-MM /
    YYYY-MM-DD) and are attached as literals on the metric's `time_column`;
    the window is REQUIRED, so the cost gate's partition requirement is met
    by construction and an unbounded scan is never compiled;
  * `limit` is an int in [1, MAX_LIMIT].

The compiled SQL then goes through the ordinary pipeline: Gate (read-only),
Semantic (trace), Govern (RLS/CLS for the persona — a metric over a denied
column is refused there, table-level deny applies), Cost (path expansion),
Execute (shaped result, `_r_<n>` when large). The compiler adds no privilege.
"""

from __future__ import annotations

import json
import re

import sqlglot
from sqlglot import exp

from .model import Metric

MAX_LIMIT = 1000
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DAY_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_GRAIN_RE = {"month": _MONTH_RE, "day": _DAY_RE}
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


class SemanticError(ValueError):
    """The call is out of scope: unregistered metric / dimension / filter,
    malformed window. The message is what the model reads — actionable, and
    never echoing the offending value back as SQL."""


def _expr(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="duckdb")


def _literal(value) -> exp.Literal:
    """A bound literal for a filter value: numbers stay numbers, everything
    else is a string literal — quoting is the AST's job, never ours."""
    if isinstance(value, bool):
        return exp.Literal.string(str(value).lower())
    if isinstance(value, int | float):
        return exp.Literal.number(value)
    s = str(value)
    if _NUM_RE.match(s):
        return exp.Literal.number(s)
    return exp.Literal.string(s)


def example(metric: Metric) -> str:
    """A concrete call the model can copy — every refusal carries one."""
    if metric.grain == "month":
        window = '{"from": "2026-07", "to": "2026-08"}'
    else:
        window = '"2026-09-02"'
    dims = (
        ""
        if metric.kind == "period_delta" or not metric.default_dimensions
        else (f", dims={list(metric.default_dimensions)!r}")
    )
    return f'Example: call_metric(metric="{metric.name}"{dims}, time_window={window})'


def _period(value, grain: str, where: str) -> str:
    s = str(value).strip()
    if not _GRAIN_RE[grain].match(s):
        want = "YYYY-MM" if grain == "month" else "YYYY-MM-DD"
        raise SemanticError(f"{where}: time_window values must look like {want}")
    return s


def time_predicate(metric: Metric, time_window) -> exp.Expression:
    """`{"eq": p}`, `{"from": a, "to": b}` (inclusive) or `[p, ...]` → predicate
    on the metric's time column, with literals of the grain's exact shape."""
    col = exp.column(metric.time_column)
    where = f"metric {metric.name!r}"
    if isinstance(time_window, str):
        return exp.EQ(
            this=col, expression=exp.Literal.string(_period(time_window, metric.grain, where))
        )
    if isinstance(time_window, list | tuple):
        if not time_window:
            raise SemanticError(f"{where}: time_window list is empty")
        vals = [exp.Literal.string(_period(v, metric.grain, where)) for v in time_window]
        return (
            exp.In(this=col, expressions=vals)
            if len(vals) > 1
            else exp.EQ(this=col, expression=vals[0])
        )
    if isinstance(time_window, dict):
        if "eq" in time_window:
            return exp.EQ(
                this=col,
                expression=exp.Literal.string(_period(time_window["eq"], metric.grain, where)),
            )
        if "from" in time_window and "to" in time_window:
            lo = _period(time_window["from"], metric.grain, where)
            hi = _period(time_window["to"], metric.grain, where)
            if lo > hi:
                raise SemanticError(f"{where}: time_window from > to")
            return exp.Between(this=col, low=exp.Literal.string(lo), high=exp.Literal.string(hi))
    raise SemanticError(
        f"{where}: time_window is required — a period string, a list of periods, "
        f'{{"eq": p}} or {{"from": a, "to": b}}. {example(metric)}'
    )


def _filters(metric: Metric, filters) -> list[exp.Expression]:
    if not filters:
        return []
    if not isinstance(filters, dict):
        raise SemanticError(f"metric {metric.name!r}: filters must be an object {{column: value}}")
    preds: list[exp.Expression] = []
    for col, value in filters.items():
        if col not in metric.filters:
            allowed = ", ".join(metric.filters) or "(none)"
            raise SemanticError(
                f"metric {metric.name!r}: filter column not registered — allowed: {allowed}"
            )
        fold = col in metric.case_insensitive_filters
        c = exp.Lower(this=exp.column(col)) if fold else exp.column(col)
        wrap = (lambda lit: exp.Lower(this=lit)) if fold else (lambda lit: lit)
        if isinstance(value, list | tuple):
            if not value:
                raise SemanticError(f"metric {metric.name!r}: filter {col!r} has no values")
            preds.append(exp.In(this=c, expressions=[wrap(_literal(v)) for v in value]))
        else:
            preds.append(exp.EQ(this=c, expression=wrap(_literal(value))))
    return preds


def _maybe_json(value):
    """A model often sends a JSON-encoded argument where an object or a list is
    declared (`'{"from": "2026-07", "to": "2026-08"}'`, `'["service"]'`). Parse
    that shape before validating, so a well-formed call is not refused for its
    encoding — measured on the cloud run of 2026-09-10, where `mom_delta` was
    refused because the window arrived as a string."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text[:1] in ("{", "[") and text[-1:] in ("}", "]"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return value


def compile(  # noqa: A001 — the module is the verb
    metric: Metric,
    dims: list[str] | tuple[str, ...] | None = None,
    time_window=None,
    filters: dict | None = None,
    limit: int = 20,
) -> str:
    """Compile one call into DuckDB SQL. Raises SemanticError when out of scope."""
    dims = _maybe_json(dims)
    time_window = _maybe_json(time_window)
    filters = _maybe_json(filters)
    if dims is None:
        dims = list(metric.default_dimensions)
    if isinstance(dims, str):
        dims = [dims]
    if not isinstance(dims, list | tuple) or not all(isinstance(d, str) for d in dims):
        raise SemanticError(
            f"metric {metric.name!r}: dims must be a list of dimension names. {example(metric)}"
        )
    if metric.kind == "period_delta":
        # the period IS the dimension of a delta metric; naming it is harmless
        dims = [d for d in dims if d != metric.time_column]
    unknown = [d for d in dims if d not in metric.dimensions]
    if unknown:
        allowed = ", ".join(metric.dimensions) or "(none — omit dims)"
        raise SemanticError(
            f"metric {metric.name!r}: dimension(s) not registered ({len(unknown)}) — "
            f"allowed: {allowed}. {example(metric)}"
        )
    try:
        limit = int(limit)
    except (TypeError, ValueError) as e:
        raise SemanticError(f"metric {metric.name!r}: limit must be an integer") from e
    if not 1 <= limit <= MAX_LIMIT:
        raise SemanticError(f"metric {metric.name!r}: limit must be between 1 and {MAX_LIMIT}")

    where = [time_predicate(metric, time_window)]
    where += [_expr(p) for p in metric.fixed_filters]
    where += _filters(metric, filters)
    condition = where[0]
    for p in where[1:]:
        condition = exp.And(this=condition, expression=p)
    measure = exp.alias_(_expr(metric.measure_sql), metric.measure_alias)

    if metric.kind == "period_delta":
        return _period_delta(metric, condition, measure, limit)

    dim_exprs = [exp.alias_(_expr(metric.dimensions[d]), d) for d in dims]
    q = exp.select(*dim_exprs, measure).from_(exp.to_table(metric.table)).where(condition)
    if dims:
        q = q.group_by(*[exp.Literal.number(i + 1) for i in range(len(dims))])
        q = q.order_by(exp.Ordered(this=exp.column(metric.measure_alias), desc=True))
    return q.limit(limit).sql(dialect="duckdb")


def _period_delta(metric: Metric, condition, measure, limit: int) -> str:
    """Per-period totals and the change against the previous period."""
    t = exp.column(metric.time_column)
    inner = (
        exp.select(t, measure)
        .from_(exp.to_table(metric.table))
        .where(condition)
        .group_by(exp.Literal.number(1))
    )
    m = exp.column(metric.measure_alias)
    lag = exp.Window(
        this=exp.Anonymous(this="lag", expressions=[m]),
        order=exp.Order(expressions=[exp.Ordered(this=t)]),
    )
    delta = exp.alias_(exp.Sub(this=m, expression=lag), "delta")
    pct = exp.alias_(
        _expr(
            f"round(100.0 * ({metric.measure_alias} - lag({metric.measure_alias}) OVER "  # nosec B608 - registry identifiers only
            f"(ORDER BY {metric.time_column})) / lag({metric.measure_alias}) OVER "
            f"(ORDER BY {metric.time_column}), 1)"
        ),
        "pct_change",
    )
    outer = (
        exp.select(t, m, delta, pct)
        .from_(exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier("p"))))
        .order_by(exp.Ordered(this=t))
    )
    return outer.limit(limit).sql(dialect="duckdb")
