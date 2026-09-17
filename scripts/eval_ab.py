#!/usr/bin/env python3
"""Compare two eval result JSON files (A/B: with vs without search_schema,
with vs without call_metric, before vs after a change) case by case.

  python scripts/eval_ab.py docs/eval-results.json /tmp/no-search-schema.json \
      --label-a with --label-b without
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _mean(xs):
    return round(sum(xs) / len(xs), 1) if xs else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    ap.add_argument("--category", default="", help="restrict to one category")
    args = ap.parse_args()
    a = {r["id"]: r for r in json.loads(Path(args.a).read_text())}
    b = {r["id"]: r for r in json.loads(Path(args.b).read_text())}
    ids = [i for i in a if i in b and (not args.category or a[i]["category"] == args.category)]
    if not ids:
        raise SystemExit("no common cases")
    la, lb = args.label_a, args.label_b
    print(
        f"| Case | {la} pass | {lb} pass | SQL calls {la}→{lb} | in-tokens {la}→{lb} | Δ tokens |"
    )
    print("|---|---|---|---|---|---|")
    for i in ids:
        ra, rb = a[i], b[i]
        ta, tb = ra.get("input_tokens", 0), rb.get("input_tokens", 0)
        delta = f"{(tb - ta) / ta:+.0%}" if ta else "n/a"
        print(
            f"| {i} | {'✅' if ra['passed'] else '❌'} | {'✅' if rb['passed'] else '❌'} | "
            f"{ra['tool_calls']}→{rb['tool_calls']} | {ta:,}→{tb:,} | {delta} |"
        )
    pa = sum(a[i]["passed"] for i in ids)
    pb = sum(b[i]["passed"] for i in ids)
    ma, mb = _mean([a[i]["input_tokens"] for i in ids]), _mean([b[i]["input_tokens"] for i in ids])
    ca, cb = _mean([a[i]["tool_calls"] for i in ids]), _mean([b[i]["tool_calls"] for i in ids])
    d = f"{(mb - ma) / ma:+.0%}" if ma else "n/a"
    print(
        f"| **{len(ids)} cases** | {pa}/{len(ids)} | {pb}/{len(ids)} | {ca}→{cb} | "
        f"{ma:,.0f}→{mb:,.0f} | {d} |"
    )


if __name__ == "__main__":
    main()
