"""The Storage-layer registry: every scenario's contribution, merged.

One place answers "what can the engine mount, what must the cost gate guard,
what does the prompt say, what policies ship by default". A new scenario is
one module under `scenarios/` plus one entry in SCENARIOS.
"""

from __future__ import annotations

from collections.abc import Iterable

from . import legs
from .scenarios import cloudops, onchain
from .scenarios.base import Scenario

SCENARIOS: tuple[Scenario, ...] = (onchain.SCENARIO, cloudops.SCENARIO)


def enabled(scenarios: Iterable[Scenario] | None = None) -> list[Scenario]:
    return [s for s in (SCENARIOS if scenarios is None else scenarios) if s.enabled]


def view_defs(scenarios: Iterable[Scenario] | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in enabled(scenarios):
        out.update(s.views)
    return out


def partitioned_sources(scenarios: Iterable[Scenario] | None = None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in enabled(scenarios):
        out.update(s.sources)
    return out


def default_policies(scenarios: Iterable[Scenario] | None = None) -> dict:
    """Every scenario's personas, mounted or not (a policy for an absent table
    is harmless and fails closed)."""
    out: dict = {}
    for s in SCENARIOS if scenarios is None else scenarios:
        out.update(s.policies)
    return out


def catalog_doc(active_legs: list[str], scenarios: Iterable[Scenario] | None = None) -> str:
    """The data section of the system prompt: each enabled scenario's doc,
    then the active catalog legs (raw views are always present)."""
    parts = [s.doc for s in enabled(scenarios) if s.doc]
    parts.append(legs.doc_section(active_legs))
    return "".join(parts)


# The cost gate (`context.cost`) and the data map (`context.datamap`) read this
# at import: raw partitioned sources of every enabled scenario.
PARTITIONED_SOURCES: dict[str, dict] = partitioned_sources()
