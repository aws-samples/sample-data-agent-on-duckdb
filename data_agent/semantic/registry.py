"""The metric catalog (semantic layer v0).

A metric is a reviewed definition — "what does *unblended cost by service*
mean here" — that compiles to SQL deterministically instead of being
re-derived by the model on every question. The catalog is delivered by the
scenarios (`Scenario.metrics`, one `Metric` per stable KPI) and loaded at cold
start; there is no runtime registration.

Naming follows a subset of the Apache Ossie v0.1 metric fields (name, version,
owner, measure, dimensions, time column, grain, filters); the full standard is
not promised. Everything in a `Metric` is operator-authored configuration, not
model input, and is validated at import like the governance policies: a
malformed metric never serves a question.

Visibility: `visible_metrics()` lists only the metrics whose table the active
persona may read (`governance.visible_tables()`), so the prompt and the tool
never advertise a KPI the rewrite would refuse — the same discipline as the
L0 table map. Refusing an unregistered metric or dimension is the
layer's whole point: an answer with the wrong cost basis is worse than none.
"""

from __future__ import annotations

from data_agent import governance
from data_agent.storage import registry as storage_registry

from .model import Metric, MetricError, validate


def load(scenarios=None) -> dict[str, Metric]:
    """Every scenario's metrics, validated, keyed by name (mounted or not: a
    metric over an absent table fails closed at the engine, like a policy)."""
    out: dict[str, Metric] = {}
    for sc in storage_registry.SCENARIOS if scenarios is None else scenarios:
        for name, m in (getattr(sc, "metrics", None) or {}).items():
            if name != m.name:
                raise MetricError(f"metric key {name!r} != metric.name {m.name!r}")
            if name in out:
                raise MetricError(f"metric {name!r} is defined by two scenarios")
            validate(m)
            out[name] = m
    return out


# Loaded on first use, not at import: the scenarios (which declare the
# metrics) import `semantic.model`, and this module imports the storage
# registry — resolving at call time keeps that a DAG.
_cache: dict[str, dict[str, Metric]] = {}


def metrics() -> dict[str, Metric]:
    if "all" not in _cache:
        _cache["all"] = load()
    return _cache["all"]


def reset() -> None:
    """Forget the loaded catalog (tests that swap scenarios)."""
    _cache.clear()


def get(name: str) -> Metric | None:
    return metrics().get(name)


def visible_metrics() -> list[Metric]:
    """Metrics whose table the active persona may read (None = all)."""
    vis = governance.visible_tables()
    return [m for m in metrics().values() if vis is None or m.table in vis]


def catalog_doc(ms: list[Metric] | None = None) -> str:
    """The prompt section listing the callable metrics for this persona."""
    ms = visible_metrics() if ms is None else ms
    if not ms:
        return ""
    lines = [
        "## Registered metrics (call_metric) — stable KPIs, compiled deterministically",
        "",
        "| Metric | Meaning | Dimensions | Time column (grain) |",
        "|---|---|---|---|",
    ]
    for m in ms:
        dims = ", ".join(m.dimensions) or "(none — omit dims)"
        lines.append(
            f"| `{m.name}` v{m.version} | {m.description} | {dims} | "
            f"`{m.time_column}` ({m.grain}) |"
        )
    from .compile import example

    lines += ["", "Call shapes: " + "; ".join(example(m) for m in ms[:3])]
    lines += [
        "",
        "For a stable KPI, call `call_metric(metric, dims, time_window, filters)` first: the "
        "definition is reviewed, the SQL is compiled (not written), the result is governed like "
        "any statement. An unregistered metric or dimension is refused — say so, do not "
        "improvise the cost basis. Exploratory questions still go through run_sql.",
    ]
    return "\n".join(lines)
