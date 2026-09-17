#!/usr/bin/env python3
"""Live three-way reconciliation: prompt doc ↔ table cards ↔ the real engine.

The unit suite (tests/unit/test_reconcile.py) checks doc and policy column
names against the cards with zero network. This script closes the third side
against the engine that will actually serve queries: it starts the in-process
DuckDB exactly as the agent does (scenario views from env, catalog legs from
env), DESCRIBEs every mounted table the way the data map does (one bounded
single-partition probe for raw sources, catalog metadata for legs) and reports
every drift. Exit code 1 on any drift — run it on AWS-side compute after a
schema change, a loader run, or a DuckDB upgrade.

  S3_TABLES_ARN=... GLUE_CATALOG=... DUCKLAKE_CATALOG=... OPS_PREFIX=... \\
      python scripts/reconcile.py [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HOME", "/tmp")  # nosec B108 - DuckDB extension dir on SSM shells
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_agent.compute import engine  # noqa: E402
from data_agent.context import datamap, reconcile  # noqa: E402
from data_agent.storage import registry  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args()

    t0 = time.perf_counter()
    conn = engine.get_conn()
    init_s = round(time.perf_counter() - t0, 1)
    entries = {
        e["table"].lower(): e for e in datamap.catalog_entries(conn) if e["database"] == "main"
    }
    legs = [e for e in datamap.catalog_entries(conn) if e["database"] != "main"]
    described: dict[str, list[str]] = {}

    def engine_columns(table: str) -> set[str] | None:
        e = entries.get(table.lower())
        if e is None:
            if table in engine.VIEW_ERRORS:
                raise RuntimeError(f"view not mounted: {engine.VIEW_ERRORS[table]}")
            return None  # not mounted in this deployment: nothing to compare
        cols = [r[0] for r in conn.execute(e["probe"]).fetchall()]
        described[table] = cols
        return set(cols)

    drifts = reconcile.check(registry.SCENARIOS, engine_columns)
    # catalog-leg copies of a raw table: every column the leg exposes must be on the raw card
    for e in legs:
        card = registry.CARDS.get(e["table"].lower())
        if card is None:
            drifts.append(reconcile.Drift("no_card", e["name"], "catalog-leg table has no card"))
            continue
        cols = {r[0].lower() for r in conn.execute(e["probe"]).fetchall()}
        described[e["name"]] = sorted(cols)
        extra = sorted(cols - {c.lower() for c in card.columns})
        if extra:
            drifts.append(reconcile.Drift("undocumented_column", e["name"], f"leg exposes {extra}"))
    report = {
        "engine_init_s": init_s,
        "tables_described": len(described),
        "columns": described,
        "view_errors": engine.VIEW_ERRORS,
        "drifts": [str(d) for d in drifts],
    }
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(f"engine init {init_s}s; described {len(described)} tables")
        for name, cols in described.items():
            print(f"  {name}: {len(cols)} cols")
        for name, err in engine.VIEW_ERRORS.items():
            print(f"  NOT MOUNTED {name}: {err}")
        print("drifts:", len(drifts))
        for d in drifts:
            print("  -", d)
    sys.exit(1 if drifts else 0)


if __name__ == "__main__":
    main()
