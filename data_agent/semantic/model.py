"""The metric definition and its validation — a leaf module (imports nothing
from the rest of the package) so scenarios can declare metrics without an
import cycle through governance / storage."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
KINDS = ("aggregate", "period_delta")


@dataclass(frozen=True)
class Metric:
    """One registered KPI.

    measure_sql:  the aggregate expression (e.g. `round(sum(x), 2)`); parsed
                  once at import, must be a single expression with no subquery.
    dimensions:   allowed group-by dimensions: name → SQL expression (a bare
                  column, or a fixed expression such as
                  `coalesce(session_issuer_arn, user_arn)`).
    time_column:  the partition/time column; every call must bound it (the
                  cost gate requires it anyway, the compiler makes it explicit).
    grain:        `month` (YYYY-MM literals) or `day` (YYYY-MM-DD).
    filters:      columns a caller may filter on (values become bound
                  literals, never interpolated); `fixed_filters` are applied
                  always (e.g. `event_name = 'AssumeRole'`).
    kind:         `aggregate` (group by dims) or `period_delta` (per-period
                  totals plus the change against the previous period).
    """

    name: str
    version: str
    owner: str
    description: str
    table: str
    measure_sql: str
    measure_alias: str
    time_column: str
    grain: str
    dimensions: dict[str, str] = field(default_factory=dict)
    default_dimensions: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    fixed_filters: tuple[str, ...] = ()
    kind: str = "aggregate"

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


class MetricError(ValueError):
    """A malformed metric definition (operator error, fail-stop at import)."""


def _single_expression(sql: str, where: str) -> exp.Expression:
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception as e:  # noqa: BLE001 — any parse failure is a bad definition
        raise MetricError(f"{where}: does not parse ({e})") from e
    if tree.find(exp.Select, exp.Subquery, exp.Command):
        raise MetricError(f"{where}: must be a plain expression, not a query")
    return tree


def validate(m: Metric) -> None:
    """Fail-stop on a malformed metric (mirrors governance._validate_policies)."""
    where = f"metric {m.name!r}"
    if not _IDENT_RE.match(m.name):
        raise MetricError(f"{where}: name must be a bare identifier")
    if not _VERSION_RE.match(m.version):
        raise MetricError(f"{where}: version must look like 1.0 or 1.0.0")
    if not m.owner or not m.description:
        raise MetricError(f"{where}: owner and description are required")
    if not _IDENT_RE.match(m.table):
        raise MetricError(f"{where}: table must be a bare identifier (governance matches it)")
    if not _IDENT_RE.match(m.measure_alias) or not _IDENT_RE.match(m.time_column):
        raise MetricError(f"{where}: measure_alias and time_column must be bare identifiers")
    if m.grain not in ("month", "day"):
        raise MetricError(f"{where}: grain must be month or day")
    if m.kind not in KINDS:
        raise MetricError(f"{where}: kind must be one of {KINDS}")
    _single_expression(m.measure_sql, f"{where}: measure_sql")
    for dim, expr in m.dimensions.items():
        if not _IDENT_RE.match(dim):
            raise MetricError(f"{where}: dimension {dim!r} must be a bare identifier")
        _single_expression(expr, f"{where}: dimension {dim!r}")
    for d in m.default_dimensions:
        if d not in m.dimensions:
            raise MetricError(f"{where}: default dimension {d!r} is not a dimension")
    for col in m.filters:
        if not _IDENT_RE.match(col):
            raise MetricError(f"{where}: filter column {col!r} must be a bare identifier")
    for pred in m.fixed_filters:
        probe = sqlglot.parse_one(f"SELECT * FROM t WHERE {pred}", read="duckdb")  # nosec B608 - validation-only parse
        if not isinstance(probe, exp.Select) or probe.args.get("where") is None:
            raise MetricError(f"{where}: fixed filter {pred!r} must be a WHERE predicate")
