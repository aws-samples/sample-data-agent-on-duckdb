# eth_traces — notes

- A contract-mediated drain appears ONLY here (internal calls). eth_transactions
  holds the outer, externally signed call; the value that moved through the
  contract is on the trace rows. Trace a theft from this table.
- `value` is wei. Divide by 1e18 for ETH, and filter `value > 0` when the
  question is about money moving — most trace rows are zero-value calls.
- Count only successful calls: `status = 1`. A reverted call moved nothing.
- Addresses are lowercase hex. Compare with `lower(...)` when the address
  comes from a user or another table.
- Known case anchor (2025-02-21): the largest inbound transfer to the exploiter
  address is ~401,347 ETH. If a query returns something very different for that
  question, check the date window and the `status` / `value > 0` filters first.
