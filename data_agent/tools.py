"""The model-facing tools: `run_sql` over the statement pipeline and
`search_schema` over the data map. Both are thin — the behaviour lives in the
layers they compose."""

from __future__ import annotations

import asyncio
import json

from strands import tool

from data_agent import audit, governance, pipeline
from data_agent.compute import engine
from data_agent.context import datamap

_DONE = object()


@tool
async def run_sql(sql: str):
    """Run a DuckDB SQL statement. Small results come back as JSON rows plus
    engine metrics ({"engine_ms", "rows"}). A large result is NOT truncated:
    it is kept whole in a session temp table `_r_<n>` and you receive a
    schema summary ({"shape":"summary","ref":"_r_<n>",...}) — query `_r_<n>`
    with run_sql to read it (filter, aggregate, JOIN) instead of re-running
    the scan. Allowed: read statements and CREATE TEMP TABLE/VIEW.

    Args:
        sql: A single DuckDB SQL statement.
    """
    # Async-generator tool: intermediate yields surface to the UI as tool_stream
    # events (the live gate → rewrite → cost → execute trace); the FINAL yield
    # becomes the ToolResult the model reads. The pipeline generator is pumped
    # one item at a time off the event loop, so the trace streams live while
    # the blocking engine call runs on a worker thread. The engine is
    # in-process: each phase carries the evidence (the rewritten SQL, the
    # engine_ms) with zero network hops to a query service.
    gen = pipeline.run(sql)
    while True:
        item = await asyncio.to_thread(next, gen, _DONE)
        if item is _DONE:
            return
        yield item


@tool
def search_schema(query: str, limit: int = 10) -> str:
    """Find tables by keyword when you are not sure which table holds what.
    Zero-network: ranks the engine's own catalog (table names + column names)
    with BM25. Returns JSON hits with the table's qualified name, kind,
    partition column (required in WHERE for raw sources) and known columns.
    Then DESCRIBE the table with run_sql for the exact schema.

    Args:
        query: keywords, e.g. "internal calls value address" or "sanctions list".
        limit: max hits (default 10).
    """
    try:
        hits = datamap.search(
            engine.get_conn(), engine.conn_lock, query, max(1, min(int(limit), 50))
        )
    except Exception as e:  # noqa: BLE001 — never let discovery kill the turn
        return json.dumps({"query": query, "hits": [], "error": str(e)[:300]})
    audit.record(
        principal=governance.active_role(),
        sql=query,
        outcome="executed",
        rows=len(hits),
        tool="search_schema",
    )
    return datamap.format_hits(hits, query)
