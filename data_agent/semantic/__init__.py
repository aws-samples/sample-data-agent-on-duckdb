"""Semantic layer (v0) — registered metrics compiled to SQL.

The sixth layer, inserted between the read-only gate and governance in the
statement pipeline: a stable KPI is a reviewed definition (`model.Metric`)
that `compile.compile()` turns into one deterministic SELECT; the model asks
for it by name through the `call_metric` tool instead of re-deriving the cost
basis every time. Everything downstream is unchanged — the compiled statement
is governed, cost-gated, executed and shaped like any run_sql statement, and
the audit line carries `metric@version`.

Admission (design invariant 8): the catalog is reconciled
against the in-process engine's tables (table cards), the compiled SQL goes
through Govern and Cost, and the result stays in the session working set.
Unregistered metrics and dimensions are refused (OUT_OF_SCOPE): an answer on
the wrong cost basis is worse than no answer.

Import shape: `model` and `compile` are leaves (scenarios import `model` to
declare metrics); `registry` reads the storage registry and governance, so it
is resolved lazily through `__getattr__` to keep the import graph a DAG.
"""

from .compile import MAX_LIMIT, SemanticError, compile
from .model import Metric, MetricError

_REGISTRY_NAMES = frozenset({"catalog_doc", "get", "metrics", "visible_metrics", "reset"})

__all__ = [
    "MAX_LIMIT",
    "Metric",
    "MetricError",
    "SemanticError",
    "catalog_doc",
    "compile",
    "get",
    "metrics",
    "reset",
    "visible_metrics",
]


def __getattr__(name: str):
    if name in _REGISTRY_NAMES:
        from . import registry

        return getattr(registry, name)
    raise AttributeError(name)
