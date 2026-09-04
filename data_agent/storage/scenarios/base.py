"""The contract a scenario fulfils towards the other layers."""

from __future__ import annotations

from dataclasses import dataclass, field


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
    macros: tuple[str, ...] = ()
    lenient: bool = False

    @property
    def enabled(self) -> bool:
        """A scenario with nothing to mount contributes policies only."""
        return bool(self.views)
