#!/usr/bin/env python3
"""Governance coverage matrix report: persona × access path × shape.

Runs tests/eval/gov_matrix.py offline (local engine built from the table
cards, zero tokens) and writes docs/governance-matrix.md. `--all-paths` adds
the two compatibility legs (glue, dl) to the two primary paths (raw, s3t);
`--with-mcp` adds the MCP surface, `--with-tool` the tool path (a Gateway
result landed as a handle and joined with the governed table).

  python scripts/run_matrix.py              # 6 × 2 × 5 + deny cells
  python scripts/run_matrix.py --all-paths  # 6 × 4 × 5 + deny cells
  python scripts/run_matrix.py --all-paths --with-mcp --with-tool   # every path
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

os.environ.setdefault("AUDIT_LOG", "0")  # 125 EMF lines are noise here
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "eval"))

import gov_matrix as gm  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all-paths", action="store_true")
    ap.add_argument(
        "--with-mcp",
        action="store_true",
        help="add the MCP surface as a fifth path (identity from a token, same pipeline)",
    )
    ap.add_argument(
        "--with-tool",
        action="store_true",
        help="add the tool path (a Gateway result landed as an owner-bound handle, joined "
        "with the governed table) plus one foreign-handle cell per persona",
    )
    ap.add_argument("--out", default=str(ROOT / "docs" / "governance-matrix.md"))
    args = ap.parse_args()
    paths = gm.ALL_PATHS if args.all_paths else gm.PRIMARY_PATHS
    if args.with_mcp:
        paths = (*paths, gm.MCP_PATH)
    if args.with_tool:
        paths = (*paths, gm.TOOL_PATH)
    rows = gm.run_matrix(paths)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%MZ")
    core = [r for r in rows if r["shape"] != "deny"]
    deny = [r for r in rows if r["shape"] == "deny" and r["path"] == "raw"]
    foreign = [r for r in rows if r["expected"] == "foreign_handle"]
    srcdeny = [r for r in rows if r["expected"] == "source_denied"]
    extra = (
        f", plus {len(foreign)} foreign-handle cells (a `_r_<n>` minted by another principal, "
        "one per persona)"
        if foreign
        else ""
    )
    if srcdeny:
        extra += (
            f", plus {len(srcdeny)} source-denied cells (a gateway source outside "
            "the persona's `sources` allowlist, one per restricted persona)"
        )
    tool_legend = (
        " On the `tool` path the persona's governed table is JOINed with a handle the "
        "framework landed from a Gateway tool result: the handle must land bound to the "
        "persona with every text cell marked `data:«…»`, the table beside it must get "
        "exactly the rewrite the direct path gives, and a handle another principal minted "
        "is refused before the gate (`stage=handle`). A `fetch_source` call on a source "
        "the persona's `sources` allowlist does not name is refused by the agent side "
        "before any outbound call, audited `stage=source`."
        if gm.TOOL_PATH in paths
        else ""
    )
    body = [
        f"# Governance coverage matrix · {stamp}",
        "",
        f"{len({r['persona'] for r in rows})} personas × {len(paths)} access paths "
        f"({', '.join(paths)}) × {len(gm.SHAPES)} statement shapes "
        f"({', '.join(gm.SHAPES)}) = {len(core)} cells, plus {len(deny)} table-level deny "
        f"cells (one unlisted table per restricted persona){extra}. Every cell is one "
        "statement through the real gate → govern → cost → execute pipeline on a local "
        "engine shaped like the deployed catalog (built from the table cards); the "
        "expected outcome is derived from the persona's policy, never hand-written. Zero "
        "network, zero tokens — the same cells run as `tests/unit/test_gov_matrix.py` in CI.",
        "",
        gm.render(rows, paths),
        "",
        "Legend: a cell passes when the `rewrite` trace event matches the policy — the "
        "unrestricted role passes unchanged; a table named with a rule gets its row filter "
        "and/or column exclusion and executes; a table named with `{}` passes unchanged; an "
        "unlisted table is refused before the engine (`not in the access policy`)." + tool_legend,
    ]
    Path(args.out).write_text("\n".join(body) + "\n", encoding="utf-8")
    ok = sum(r["ok"] for r in rows)
    print(f"{ok}/{len(rows)} cells pass → {args.out}")
    sys.exit(0 if ok == len(rows) else 1)


if __name__ == "__main__":
    main()
