"""The contract a scenario fulfils towards the other layers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TableCard:
    """What a table IS, for the model and for reconciliation.

    The hand-written prompt doc, the governance policy and the engine's real
    schema drift apart silently (a production three-way check caught five
    drifts on its first run). A card is the third party in that check: the
    reviewed statement of a table's grain, when to use it, when not to, its
    caveats, its owner — and the column names the docs and policies are
    reconciled against (`tests/unit/test_reconcile.py`, `scripts/reconcile.py`).

    columns:    the column names the card vouches for. With `complete=True`
                this is the whole schema (engine columns not on the card are a
                drift); with `complete=False` it is the documented subset (a
                CUR export has 116 columns — the card names the ones the docs
                and policies use, and the engine may expose more).
    admin_only: the table is deliberately named by no restricted persona
                (admin-only by omission) — the reconcile test
                otherwise requires every mounted table to be reachable by at
                least one persona.
    """

    grain: str  # "one row = one ..."
    use_when: str
    avoid_when: str = ""
    caveats: str = ""
    owner: str = ""
    columns: tuple[str, ...] = ()
    complete: bool = True
    admin_only: bool = False


@dataclass(frozen=True)
class Scenario:
    """What one data domain contributes to the Storage layer.

    views:    view name → relation SQL (`read_parquet(...)` etc.); mounted at
              engine init as `CREATE OR REPLACE VIEW <name> AS SELECT * FROM <src>`.
    sources:  raw partitioned sources → cost-gate spec (partition column,
              granularity, base prefix, per-partition layout, reader, args).
              The cost gate requires a predicate on the column and expands
              literal predicates into explicit paths; the data map reads the
              column for the L0 table map.
    doc:      the system-prompt section describing the tables (empty = none).
    policies: default governance policies this domain ships (tenant:role →
              policy), used when GOVERNANCE_POLICIES is not set. Always
              contributed, even when the data is not mounted — a policy for an
              absent table is harmless and fails closed.
    cards:    table name → TableCard: grain / use-when / caveats and the
              reconciled column list; feeds the L0 map and `search_schema`,
              and is what doc + policy column names are checked against.
    metrics:  metric name → semantic.registry.Metric: the stable KPIs
              this domain registers for `call_metric`; validated at import.
    macros:   extra DDL run at engine init (table macros over native files).
    lenient:  True → a view whose dataset has not landed yet is skipped and
              recorded (`compute.engine.VIEW_ERRORS`) instead of failing init.
              DuckDB binds a view at creation, so an optional dataset must not
              take the whole engine down with it.
    """

    name: str
    views: dict[str, str] = field(default_factory=dict)
    sources: dict[str, dict] = field(default_factory=dict)
    doc: str = ""
    policies: dict = field(default_factory=dict)
    cards: dict[str, TableCard] = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)  # name → semantic.registry.Metric
    macros: tuple[str, ...] = ()
    lenient: bool = False

    @property
    def enabled(self) -> bool:
        """A scenario with nothing to mount contributes policies only."""
        return bool(self.views)
