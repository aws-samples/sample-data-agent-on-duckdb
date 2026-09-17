#!/usr/bin/env python3
"""Ask the deployed agent one (or several) questions as a persona, and print the
live pipeline trace next to the answer — the terminal equivalent of the UI's
Trace tab.

The production auth path: log in to Cognito as the persona's user, POST to the
runtime's /invocations with `Authorization: Bearer <ID token>`, read the SSE
envelopes. What you see per statement is the evidence, not a summary:

  gate      → the SQL the model wrote (and whether the read-only gate allowed it)
  semantic  → the registered metric a `call_metric` statement was compiled from
  rewrite   → the RLS predicate and the excluded columns for THIS persona
  cost      → the partition filter and how many explicit paths it expanded to
  execute   → engine ms, rows, shape, the `_r_<n>` handle when one was minted

  source: scripts/demo_env.sh   (exports the runtime ARN, pool, client, secrets)
  python scripts/agent_ask.py <persona> "question" ["follow-up" ...]
  python scripts/agent_ask.py --list
  python scripts/agent_ask.py junior "..." --quiet     # answer only
  python scripts/agent_ask.py --session-per-turn ...   # fresh session each turn

Personas come from tests/eval/personas.yaml (admin, investigator, junior,
finops, secops, secops_junior).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_eval  # noqa: E402

TRACE_KEYS = {
    "gate": ("allowed", "handle"),
    "semantic": ("metric", "version", "rejected", "reason"),
    "rewrite": ("governed", "principal", "rls", "cls", "rejected", "reason"),
    "cost": ("sources", "partition_filter", "expanded_paths", "budget", "rejected", "reason"),
    "execute": ("engine_ms", "rows", "shape", "materialized", "error", "hint"),
}


def _fmt(event: dict) -> str:
    phase = event.get("phase", "?")
    parts = []
    for key in TRACE_KEYS.get(phase, ()):
        if key in event and event[key] not in (None, [], False):
            value = event[key]
            if key in ("rls", "cls"):
                value = ", ".join(
                    f"{r.get('table')}.{r.get('column')}"
                    if "column" in r
                    else f"{r.get('table')}: {r.get('predicate')}"
                    for r in value
                )
            parts.append(f"{key}={value}")
    line = f"  [{phase:8}] " + " · ".join(str(p)[:160] for p in parts)
    if phase == "gate" and event.get("sql"):
        line += "\n" + "\n".join(
            f"             | {ln}" for ln in event["sql"].strip().splitlines()[:12]
        )
    return line


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("persona", nargs="?")
    ap.add_argument("prompts", nargs="*")
    ap.add_argument("--list", action="store_true", help="list the personas and exit")
    ap.add_argument("--quiet", action="store_true", help="answer only, no trace")
    ap.add_argument("--session-per-turn", action="store_true", help="new session id per prompt")
    args = ap.parse_args()

    personas = run_eval.load_personas()
    if args.list or not args.persona:
        print("personas (name → governance role):")
        for name, spec in personas.items():
            print(f"  {name:14} {spec['role']}")
        return
    if args.persona not in personas:
        sys.exit(f"unknown persona {args.persona!r}; try --list")
    if not args.prompts:
        sys.exit("give at least one question")
    if not os.environ.get("AGENT_RUNTIME_ARN"):
        sys.exit("AGENT_RUNTIME_ARN is unset — run `source scripts/demo_env.sh` first")

    turns = run_eval.run_cloud(
        list(args.prompts), personas[args.persona]["user"], args.session_per_turn
    )
    for i, turn in enumerate(turns, 1):
        head = f"── turn {i}/{len(turns)} · {args.persona} ({personas[args.persona]['role']})"
        print(f"\n{head} · HTTP {turn.http} · {turn.wall_s}s")
        if not args.quiet:
            for event in turn.trace:
                print(_fmt(event))
        print("\n" + turn.answer.strip())
        if turn.usage:
            print(f"\n  usage: {json.dumps(turn.usage)}")
    total = sum(t.usage.get("input_tokens", 0) for t in turns)
    print(f"\n{len(turns)} turn(s), {total:,} input tokens total")


if __name__ == "__main__":
    main()
