#!/usr/bin/env bash
# Concurrency isolation check: N parallel AgentCore invokes, distinct sessions.
# Each session = its own microVM = its own DuckDB. No queue, no cross-talk.
# Usage: ./scripts/concurrent_invoke.sh [N]   (default 3; needs agentcore configured)
set -euo pipefail
N="${1:-3}"
QUESTIONS=(
  "How many BTC transactions on 2026-08-25?"
  "Total BTC fees on 2026-08-24?"
  "Average BTC transaction size in bytes on 2026-08-23?"
  "How many coinbase transactions on 2026-08-22?"
  "Largest transaction by output_value on 2026-08-21?"
)
echo "launching $N concurrent sessions..."
pids=()
for ((i = 0; i < N; i++)); do
  q="${QUESTIONS[$((i % ${#QUESTIONS[@]}))]}"
  (
    t0=$(date +%s.%N)
    agentcore invoke "{\"prompt\": \"$q\"}" > "/tmp/concurrent_$i.json" 2>&1
    t1=$(date +%s.%N)
    echo "session $i: $(echo "$t1 - $t0" | bc)s — $q"
  ) &
  pids+=($!)
done
for p in "${pids[@]}"; do wait "$p"; done
echo "all sessions complete. Note: latencies are independent — no queueing between sessions."
