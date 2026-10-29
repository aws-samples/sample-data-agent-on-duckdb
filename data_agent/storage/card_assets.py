"""Table cards as customer-owned Git assets (`semantic/cards/<scenario>.yaml`).

Same discipline as the metric definitions (`semantic/assets.py`): the single
source of truth is an open-format file in the customer's repository, changed
through review and the asset regression gate; this module loads it into the
internal representation (`scenarios.base.TableCard`) at import.

Fail-closed: a missing file, an unparseable document, an unknown or misspelt
field, a non-list `columns`, a missing `grain` / `use_when`, or a duplicate
table name raises `CardError` — a scenario whose cards cannot be trusted does
not mount, because the reconcile checks and the data map read them.
`SEMANTIC_ASSETS_DIR` overrides the directory (tests, split repos), as for
the metric assets.

A leaf: imports only `scenarios.base`, so scenario modules can call it.
"""

from __future__ import annotations

import os
from dataclasses import fields
from pathlib import Path

import yaml

from .scenarios.base import TableCard

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "semantic"
_FIELDS = {f.name for f in fields(TableCard)}
_REQUIRED = ("grain", "use_when")


class CardError(ValueError):
    """A malformed table-card asset (operator error, fail-stop at import)."""


def cards_dir() -> Path:
    return Path(os.environ.get("SEMANTIC_ASSETS_DIR") or _DEFAULT_DIR) / "cards"


def load_cards(scenario: str) -> dict[str, TableCard]:
    """`semantic/cards/<scenario>.yaml` → validated cards keyed by table name."""
    path = cards_dir() / f"{scenario}.yaml"
    where = f"table-card asset {path.name}"
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise CardError(
            f"{where}: not found under {path.parent} — table cards are Git assets and "
            "ship with the deployment"
        ) from e
    except yaml.YAMLError as e:
        raise CardError(f"{where}: does not parse ({e})") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("cards"), dict):
        raise CardError(f"{where}: must be a mapping with a `cards` mapping")
    out: dict[str, TableCard] = {}
    for table, item in doc["cards"].items():
        if not isinstance(item, dict):
            raise CardError(f"{where}: card {table!r} must be a mapping")
        unknown = set(item) - _FIELDS
        if unknown:
            raise CardError(f"{where}: card {table!r}: unknown fields {sorted(unknown)}")
        for key in _REQUIRED:
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise CardError(f"{where}: card {table!r}: `{key}` is required")
        cols = item.get("columns", [])
        if not isinstance(cols, list) or not all(isinstance(c, str) and c for c in cols):
            raise CardError(f"{where}: card {table!r}: columns must be a list of names")
        if len(set(cols)) != len(cols):
            raise CardError(f"{where}: card {table!r}: a column is listed twice")
        for key in ("complete", "admin_only"):
            if key in item and not isinstance(item[key], bool):
                raise CardError(f"{where}: card {table!r}: {key} must be true or false")
        out[table] = TableCard(**{**item, "columns": tuple(cols)})
    if not out:
        raise CardError(f"{where}: no cards")
    return out
