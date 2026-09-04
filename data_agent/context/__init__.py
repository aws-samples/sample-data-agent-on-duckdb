"""Context layer: what the model sees and what the engine is asked.

    results      result shaping — materialize, summarize, read back
    compression  measured-pressure history stubs pointing at `_r_<n>`
    datamap      L0 table map from the engine catalog + `search_schema`
    cost         cost policy as a rewrite: partition filter + path expansion
    prompt       system-prompt assembly and first-invoke L0 injection

Every mechanism here relies on the engine being in-process (design invariant
8, see docs/design.md): oversized results become engine tables, compressed history points at
those tables, the table map is read from the engine catalog.
"""
