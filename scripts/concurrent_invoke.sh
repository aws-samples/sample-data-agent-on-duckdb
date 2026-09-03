#!/usr/bin/env bash
# Concurrency isolation check: N parallel AgentCore invokes, distinct sessions.
# Each session = its own microVM = its own DuckDB. No queue, no cross-talk.
# Usage: ./scripts/concurrent_invoke.sh [N]   (default 3; needs agentcore configured)
set -euo pipefail
N="${1:-3}"
QUESTIONS=(
  "How many internal calls are in eth_traces on 2025-02-21?"
  "Total ETH moved in internal calls on 2025-02-22?"
  "How many distinct to_address values in eth_traces on 2025-02-23?"
  "Largest single internal call by value on 2025-02-21?"
  "How many failed internal calls (status=0) on 2025-02-22?"
)
echo "launching $N concurrent sessions..."
pids=()
for i in $(seq 0 $((N - 1))); do
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
