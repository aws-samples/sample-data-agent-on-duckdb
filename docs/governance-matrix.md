# Governance coverage matrix · 2026-09-17 06:17Z

6 personas × 6 access paths (raw, s3t, glue, dl, mcp, tool) × 5 statement shapes (plain, cte, setop, derived_alias, create_temp_as) = 180 cells, plus 5 table-level deny cells (one unlisted table per restricted persona), plus 6 foreign-handle cells (a `_r_<n>` minted by another principal, one per persona). Every cell is one statement through the real gate → govern → cost → execute pipeline on a local engine shaped like the deployed catalog (built from the table cards); the expected outcome is derived from the persona's policy, never hand-written. Zero network, zero tokens — the same cells run as `tests/unit/test_gov_matrix.py` in CI.

| Persona | raw | s3t | glue | dl | mcp | tool | deny (raw) | deny (tool) |
|---|---|---|---|---|---|---|---|---|
| admin | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | — | 1/1 ((foreign handle)) |
| finops | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 1/1 (cw_logs) | 1/1 ((foreign handle)) |
| investigator | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 1/1 (cw_logs) | 1/1 ((foreign handle)) |
| junior | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 1/1 (cw_logs) | 1/1 ((foreign handle)) |
| secops | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 1/1 (cw_logs) | 1/1 ((foreign handle)) |
| secops_junior | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 1/1 (cw_logs) | 1/1 ((foreign handle)) |

**191/191 cells pass** (100%).

Legend: a cell passes when the `rewrite` trace event matches the policy — the unrestricted role passes unchanged; a table named with a rule gets its row filter and/or column exclusion and executes; a table named with `{}` passes unchanged; an unlisted table is refused before the engine (`not in the access policy`). On the `tool` path the persona's governed table is JOINed with a handle the framework landed from a Gateway tool result: the handle must land bound to the persona with every text cell marked `data:«…»`, the table beside it must get exactly the rewrite the direct path gives, and a handle another principal minted is refused before the gate (`stage=handle`).
