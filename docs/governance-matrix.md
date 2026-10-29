# Governance coverage matrix · 2026-10-18 04:18Z

6 personas × 6 access paths (raw, s3t, glue, dl, mcp, tool) × 5 statement shapes (plain, cte, setop, derived_alias, create_temp_as) = 180 cells, plus 5 table-level deny cells (one unlisted table per restricted persona), plus 6 foreign-handle cells (a `_r_<n>` minted by another principal, one per persona), plus 5 source-denied cells (a gateway source outside the persona's `sources` allowlist, one per restricted persona). Every cell is one statement through the real gate → govern → cost → execute pipeline on a local engine shaped like the deployed catalog (built from the table cards); the expected outcome is derived from the persona's policy, never hand-written. Zero network, zero tokens — the same cells run as `tests/unit/test_gov_matrix.py` in CI.

| Persona | raw | s3t | glue | dl | mcp | tool | deny (raw) | deny (tool) |
|---|---|---|---|---|---|---|---|---|
| admin | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | — | 1/1 ((foreign handle)) |
| finops | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 1/1 (cw_logs) | 2/2 ((foreign handle), (source labels-api)) |
| investigator | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 1/1 (cw_logs) | 2/2 ((foreign handle), (source redshift)) |
| junior | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 1/1 (cw_logs) | 2/2 ((foreign handle), (source redshift)) |
| secops | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 1/1 (cw_logs) | 2/2 ((foreign handle), (source labels-api)) |
| secops_junior | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 1/1 (cw_logs) | 2/2 ((foreign handle), (source labels-api)) |

**196/196 cells pass** (100%).

Legend: a cell passes when the `rewrite` trace event matches the policy — the unrestricted role passes unchanged; a table named with a rule gets its row filter and/or column exclusion and executes; a table named with `{}` passes unchanged; an unlisted table is refused before the engine (`not in the access policy`). On the `tool` path the persona's governed table is JOINed with a handle the framework landed from a Gateway tool result: the handle must land bound to the persona with every text cell marked `data:«…»`, the table beside it must get exactly the rewrite the direct path gives, and a handle another principal minted is refused before the gate (`stage=handle`). A `fetch_source` call on a source the persona's `sources` allowlist does not name is refused by the agent side before any outbound call, audited `stage=source`.
