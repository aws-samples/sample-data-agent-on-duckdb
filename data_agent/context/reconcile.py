"""Three-way reconciliation: prompt doc ↔ table card ↔ engine schema.

What the model is told about a table lives in three places that drift apart
silently: the hand-written scenario doc (`Scenario.doc`), the governance
policy (row filters and denied columns name columns), and the engine's real
schema. A production three-way check caught five drifts on its first run;
a stale column name costs the model a repair round per query — or, worse for
governance, a `deny_columns` entry that no longer exists governs nothing.

The table card (`storage.scenarios.base.TableCard`) is the reviewed middle:
  * offline (unit tests, zero network): every column the doc or a policy
    names must be on the card; every mounted table has a card; every table is
    reachable by at least one restricted persona or is marked `admin_only`;
  * live (`scripts/reconcile.py` on AWS-side compute, or the same function
    with a local engine in tests): the card's columns exist in the engine,
    and — for cards marked complete — the engine exposes nothing the card
    does not know.

Everything here is a pure function over the Storage registry plus an optional
`engine_columns(table) -> set[str]` callback, so the check runs in CI without
S3 and against the real engine on the worker with the same code.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlglot import exp, parse_one

from data_agent.storage.scenarios.base import Scenario, TableCard

_IDENT_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\b")
_BACKTICK_RE = re.compile(r"`([a-z_][a-z0-9_]*)`")
# partition-column cells and prose words that survive as identifiers
_PROSE = frozenset("yyyy mm dd required in where and or the a an of as also".split())


@dataclass(frozen=True)
class Drift:
    # doc_column | policy_column | no_card | no_persona | phantom_column | undocumented_column
    kind: str
    table: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.table}: {self.detail}"


# --------------------------------------------------------------------- doc
def doc_columns(doc: str, tables: set[str]) -> dict[str, set[str]]:
    """Column names the prompt doc writes per table.

    Reads the markdown rows whose first cell is a known table name. The cell
    under a header that says "columns" (the cloud-ops "Key columns") is a
    plain identifier list and contributes every token; every other cell is
    prose (the on-chain "Notes") and contributes only what is unambiguously a
    column name — a backticked token or an identifier containing an
    underscore (a backticked name inside a parenthesised aside counts; other
    aside text is dropped). Path cells are skipped; table names are never
    counted as columns; the partition cell's backticked name is a column too.
    """
    out: dict[str, set[str]] = {}
    list_idx: int | None = None
    for line in doc.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        head = cells[0].strip("`").lower()
        if head.startswith("table"):  # header row: locate the "columns" cell
            list_idx = next(
                (
                    i
                    for i, c in enumerate(cells)
                    if "column" in c.lower() and "partition" not in c.lower()
                ),
                None,
            )
            continue
        if head not in tables:
            continue
        cols: set[str] = set()
        for i, cell in enumerate(cells[1:], start=1):
            if "://" in cell:
                continue
            text = cell.lower()
            cols.update(m.group(1) for m in _BACKTICK_RE.finditer(text))  # incl. in asides
            plain = _BACKTICK_RE.sub(" ", re.sub(r"\([^)]*\)", " ", text))
            if i == list_idx:
                cols.update(m.group(1) for m in _IDENT_RE.finditer(plain))
            else:
                cols.update(m.group(1) for m in _IDENT_RE.finditer(plain) if "_" in m.group(1))
        out[head] = {c for c in cols if c not in tables and c not in _PROSE}
    return out


def policy_columns(policies: dict) -> dict[str, set[str]]:
    """Column names every scenario policy relies on, per governed table:
    the row filter's columns and the denied columns."""
    out: dict[str, set[str]] = {}
    for policy in policies.values():
        for table, rule in (policy.get("tables") or {}).items():
            cols = out.setdefault(table.lower(), set())
            for c in rule.get("deny_columns") or []:
                cols.add(c.lower())
            pred = rule.get("row_filter")
            if pred:
                tree = parse_one(f"SELECT * FROM t WHERE {pred}", read="duckdb")  # nosec B608 - validated policy predicate, parsed only
                cols.update(c.name.lower() for c in tree.find_all(exp.Column))
    return out


def personas_naming(policies: dict) -> dict[str, set[str]]:
    """table → restricted personas that list it (an empty rule counts)."""
    out: dict[str, set[str]] = {}
    for role, policy in policies.items():
        for table in policy.get("tables") or {}:
            out.setdefault(table.lower(), set()).add(role)
    return out


# ------------------------------------------------------------------- check
def check_scenario(
    sc: Scenario, engine_columns: Callable[[str], set[str] | None] | None = None
) -> list[Drift]:
    """All drifts for one scenario (built with every optional dataset on, so
    the check covers tables that a given deployment may not mount)."""
    drifts: list[Drift] = []
    cards = {k.lower(): v for k, v in sc.cards.items()}
    tables = {v.lower() for v in sc.views}
    named = personas_naming(sc.policies)
    for table in sorted(tables):
        card = cards.get(table)
        if card is None:
            drifts.append(Drift("no_card", table, "mounted view has no TableCard"))
            continue
        if not card.admin_only and table not in named:
            drifts.append(
                Drift("no_persona", table, "no restricted persona lists it and not admin_only")
            )
        if card.admin_only and table in named:
            drifts.append(
                Drift("no_persona", table, f"admin_only but listed by {sorted(named[table])}")
            )
    card_cols = {t: {c.lower() for c in card.columns} for t, card in cards.items()}
    for table, cols in doc_columns(sc.doc, tables).items():
        missing = sorted(cols - card_cols.get(table, set()))
        if missing:
            drifts.append(
                Drift("doc_column", table, f"doc names columns not on the card: {missing}")
            )
    for table, cols in policy_columns(sc.policies).items():
        if table not in card_cols:
            continue  # a policy for a table another scenario owns: reconciled there
        missing = sorted(cols - card_cols[table])
        if missing:
            drifts.append(
                Drift("policy_column", table, f"policy uses columns not on the card: {missing}")
            )
    if engine_columns is not None:
        for table in sorted(tables & set(cards)):
            drifts.extend(_check_engine(table, cards[table], engine_columns))
    return drifts


def _check_engine(table: str, card: TableCard, engine_columns) -> list[Drift]:
    try:
        actual = engine_columns(table)
    except Exception as e:  # noqa: BLE001 — an unreachable table is a drift, not a crash
        return [Drift("phantom_column", table, f"engine could not describe it: {str(e)[:120]}")]
    if actual is None:
        return []
    actual = {c.lower() for c in actual}
    claimed = {c.lower() for c in card.columns}
    out = []
    if phantom := sorted(claimed - actual):
        out.append(
            Drift("phantom_column", table, f"card claims columns the engine lacks: {phantom}")
        )
    if card.complete and (extra := sorted(actual - claimed)):
        out.append(
            Drift("undocumented_column", table, f"engine exposes columns not on the card: {extra}")
        )
    return out


def check(scenarios, engine_columns=None) -> list[Drift]:
    out: list[Drift] = []
    for sc in scenarios:
        out.extend(check_scenario(sc, engine_columns))
    return out
