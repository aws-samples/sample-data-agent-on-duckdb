"""Data map — L0 / L1 / L2 table discovery generated from the engine.

A hand-written table list in the system prompt is fine for seven tables and a
lie at seven hundred. Production data agents with hundreds of databases arrive at the
same three-level shape used here, and drop external metadata caches once
they measure thousands of lookups per search:

* **L0 — routing context.** A compact list of every table the engine can see
  (`duckdb_tables()` / `duckdb_views()`, attached catalogs included), with the
  partition column the cost gate requires, injected into the system prompt at
  first invoke. Names only — a few KB even at hundreds of tables.
* **L1 — zero-network search.** `search_schema(query)` ranks tables with
  DuckDB's own `fts` extension (BM25 over name + column names) held in a
  session table; no network, no LLM. Column names come from one bounded
  DESCRIBE per table (a single-partition path for raw sources, catalog
  metadata for Iceberg/DuckLake), gathered lazily on first search.
* **L2 — on demand.** `DESCRIBE <table>` through `run_sql` when the model has
  picked a table (the governance schema prober already caches these).

Freshness is session-scoped by design: a table dropped mid-conversation
errors on use and the next search rebuilds the index. Invariant 8 test: this
map is *derived from the engine's catalog*, which is why it works for any
S3 layout the engine can mount.
"""

from __future__ import annotations

import json
import os
import re
import time

from data_agent.storage.registry import PARTITIONED_SOURCES

INDEX_TABLE = "_catalog"
FTS_SCHEMA = f"fts_main_{INDEX_TABLE}"
DESCRIBE_BUDGET_S = float(os.environ.get("DATAMAP_DESCRIBE_BUDGET_S", "20"))
# a partition that exists, used to DESCRIBE raw sources without listing the prefix
PROBE_PARTITION = os.environ.get("DATAMAP_PROBE_PARTITION", "2025-02-21")
# False → DESCRIBE the local relation instead of a remote single-partition path
# (unit tests; a deployment whose raw views are local files)
PROBE_REMOTE = os.environ.get("DATAMAP_PROBE_REMOTE", "1") == "1"
_SKIP_RE = re.compile(r"^(_r_\d+|_catalog|_q)$", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

_state: dict = {"built": False, "fts": None, "entries": []}


def reset() -> None:
    _state.update(built=False, fts=None, entries=[])


# ------------------------------------------------------------------ entries
def catalog_entries(conn) -> list[dict]:
    """Every user-visible table/view the engine can see, attached catalogs
    included. Framework tables (`_r_<n>`, the index itself) are hidden."""
    rows = conn.execute(
        "SELECT database_name, schema_name, table_name, 'table' FROM duckdb_tables() "
        "WHERE NOT internal AND NOT temporary "
        "UNION ALL "
        "SELECT database_name, schema_name, view_name, 'view' FROM duckdb_views() "
        "WHERE NOT internal AND NOT temporary "
        "ORDER BY 1, 2, 3"
    ).fetchall()
    out = []
    for db, schema, name, kind in rows:
        if _SKIP_RE.match(name) or schema in ("information_schema", "pg_catalog"):
            continue
        local = db in ("memory", "main", "temp") and schema in ("main", "temp")
        qualified = name if local else f"{db}.{schema}.{name}"
        spec = PARTITIONED_SOURCES.get(name.lower()) if local else None
        out.append(
            {
                "name": qualified,
                "kind": kind,
                "database": "main" if local else db,
                "schema": schema,
                "table": name,
                "partition": spec["column"] if spec else None,
                "probe": _probe_sql(qualified, spec),
            }
        )
    return out


def _probe_sql(qualified: str, spec: dict | None) -> str:
    """A DESCRIBE that binds the relation cheaply: raw partitioned sources get
    a single-partition explicit path (no whole-prefix LIST)."""
    if spec and PROBE_REMOTE:
        path = spec["layout"].format(base=spec["base"].rstrip("/"), part=PROBE_PARTITION)
        args = f", {spec['args']}" if spec.get("args") else ""
        return f"DESCRIBE SELECT * FROM {spec['reader']}('{path}'{args}) LIMIT 0"  # nosec B608 - catalog-derived literals
    return f"DESCRIBE SELECT * FROM {qualified} LIMIT 0"  # nosec B608 - identifier from duckdb_tables()


def _bare(name: str) -> str:
    """Last segment of a possibly catalog-qualified name (s3t.blockchain.x → x)."""
    return name.rsplit(".", 1)[-1].lower()


def _visible(entries: list[dict], visible: frozenset[str] | None) -> list[dict]:
    """Keep only tables the active role may query (None = unrestricted). Shows
    the model exactly what it can reach, so its routing context matches the
    rewrite's table-level deny-by-default."""
    if visible is None:
        return entries
    return [e for e in entries if _bare(e["name"]) in visible]


# --------------------------------------------------------------------- L0
def l0_doc(entries: list[dict], visible: frozenset[str] | None = None) -> str:
    """Compact routing context for the system prompt (filtered to the role)."""
    lines = [
        "## Tables the engine can see (generated from the catalog at session start)",
        "",
        "| Table | Kind | Partition column (required in WHERE) |",
        "|---|---|---|",
    ]
    for e in _visible(entries, visible):
        part = (
            f"`{e['partition']}`" if e["partition"] else "— (catalog/label table, no filter needed)"
        )
        lines.append(f"| `{e['name']}` | {e['kind']} | {part} |")
    lines += [
        "",
        "Not sure which table? Call `search_schema(query)` (keyword search over table and "
        "column names, no network). Then `DESCRIBE <table>` via run_sql for the exact schema.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------- L1
def ensure_index(conn, lock) -> dict:
    """Build the `_catalog` session table (+ fts index) once per session."""
    if _state["built"]:
        return _state
    with lock:
        entries = catalog_entries(conn)
        t0 = time.perf_counter()
        for e in entries:
            cols = ""
            if time.perf_counter() - t0 < DESCRIBE_BUDGET_S:
                try:
                    cols = " ".join(r[0] for r in conn.execute(e["probe"]).fetchall())
                except Exception:  # noqa: BLE001 — unreachable table: names-only entry
                    cols = ""
            e["columns"] = cols
        conn.execute(f"DROP TABLE IF EXISTS {INDEX_TABLE}")  # nosec B608 - fixed identifier  # nosemgrep: sqlalchemy-execute-raw-query -- fixed identifier
        conn.execute(
            f"CREATE TEMP TABLE {INDEX_TABLE} (name VARCHAR, kind VARCHAR, partition_col VARCHAR, "  # nosec B608 - fixed identifier
            "columns VARCHAR, tokens VARCHAR)"
        )
        for e in entries:
            conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- fixed identifier
                f"INSERT INTO {INDEX_TABLE} VALUES (?, ?, ?, ?, ?)",  # nosec B608 - fixed identifier, bound params
                [e["name"], e["kind"], e["partition"], e["columns"], _tokens(e)],
            )
        fts = False
        try:
            conn.execute("INSTALL fts; LOAD fts;")
            conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- fixed identifiers
                f"PRAGMA create_fts_index('{INDEX_TABLE}', 'name', 'tokens', overwrite=1)"  # nosec B608 - fixed identifiers
            )
            fts = True
        except Exception:  # noqa: BLE001 — extension unavailable: token-overlap fallback
            fts = False
        _state.update(built=True, fts=fts, entries=entries)
    return _state


def _tokens(e: dict) -> str:
    """Searchable text: name segments + column names, underscores split."""
    raw = f"{e['name']} {e.get('columns', '')} {e['kind']} {e.get('partition') or ''}"
    return " ".join(_TOKEN_RE.findall(raw.lower().replace("_", " ").replace(".", " ")))


def _strip_denied(name: str, cols: str, denied: dict[str, frozenset[str]] | None) -> str:
    """Drop CLS-denied column names from a discovery hit's column list, so the
    model is never shown a column it could not select."""
    if not denied:
        return cols
    hide = denied.get(_bare(name))
    if not hide:
        return cols
    return " ".join(c for c in cols.split() if c.lower() not in hide)


def search(
    conn,
    lock,
    query: str,
    limit: int = 10,
    visible: frozenset[str] | None = None,
    denied: dict[str, frozenset[str]] | None = None,
) -> list[dict]:
    state = ensure_index(conn, lock)
    q = " ".join(_TOKEN_RE.findall(query.lower().replace("_", " ")))
    if not q:
        return []
    # over-fetch when filtering so the role's visible tables still fill `limit`
    fetch = limit if visible is None else max(limit * 4, 50)
    with lock:
        if state["fts"]:
            rows = conn.execute(
                f"SELECT name, kind, partition_col, columns, "  # nosec B608 - fixed identifiers, bound params
                f"{FTS_SCHEMA}.match_bm25(name, ?) AS score FROM {INDEX_TABLE} "
                "WHERE score IS NOT NULL ORDER BY score DESC, name LIMIT ?",
                [q, fetch],
            ).fetchall()
            engine = "duckdb fts (bm25)"
        else:
            rows = conn.execute(
                f"SELECT name, kind, partition_col, columns, "  # nosec B608 - fixed identifiers, bound params
                f"len(list_intersect(string_split(tokens, ' '), string_split(?, ' '))) AS score "
                f"FROM {INDEX_TABLE} WHERE score > 0 ORDER BY score DESC, name LIMIT ?",
                [q, fetch],
            ).fetchall()
            engine = "token overlap (fallback: fts unavailable)"
    if visible is not None:
        rows = [r for r in rows if _bare(r[0]) in visible][:limit]
    return [
        {
            "table": r[0],
            "kind": r[1],
            "partition_column": r[2],
            "columns": _strip_denied(r[0], (r[3] or "")[:400], denied),
            "score": round(float(r[4]), 3),
            "engine": engine,
        }
        for r in rows
    ]


def format_hits(hits: list[dict], query: str) -> str:
    if not hits:
        return json.dumps(
            {
                "query": query,
                "hits": [],
                "hint": "no table matched; try other keywords or DESCRIBE a table you know",
            }
        )
    return json.dumps(
        {"query": query, "hits": hits, "engine": hits[0]["engine"]}, ensure_ascii=False
    )
