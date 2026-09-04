"""Result shaping — materialize, summarize, read back.

Principle (invariant 9): **shape, never truncate**. A tool result the model
should not read whole is stored in full and shown in a different shape, never
as "the first N rows" — a head-truncated list reads as complete and misleads
the model (a failure mode measured in production data agents).

Because the engine is in-process, "storing" is one statement:

    CREATE TEMP TABLE _r_<n> AS <governed_sql> LIMIT <cap>

The rows never leave the engine. Small results come back inline (the temp
table is dropped again); oversized results stay materialized and the model
receives a schema summary — row count, columns/types, a few labelled sample
rows and the retrieval path — and reads back with ordinary `run_sql` against
`_r_<n>`: governed rows already, zero copies, sub-millisecond.

The same mechanism is what lets history compression replace an old
tool result with a one-line pointer without losing anything.
"""

from __future__ import annotations

import json
import os
import re
import time

import sqlglot
from sqlglot import exp

# Knobs (see docs/design.md). Defaults come from measured production failure modes.
MAX_RESULT_ROWS = int(
    os.environ.get("MAX_RESULT_ROWS", os.environ.get("QUERY_TIMEOUT_ROWS", "200"))
)
MAX_RESULT_CHARS = int(os.environ.get("MAX_RESULT_CHARS", "10000"))
MAX_MATERIALIZE_ROWS = int(os.environ.get("MAX_MATERIALIZE_ROWS", "1000000"))
RESULT_TABLE_LRU = int(os.environ.get("RESULT_TABLE_LRU", "100"))
SAMPLE_ROWS = int(os.environ.get("SAMPLE_ROWS", "3"))
SAMPLE_VALUE_CHARS = 120  # long strings in the *sample* are shortened (the table is whole)
RESULT_PREFIX = "_r_"

_QUERY_ROOTS = (exp.Select, exp.SetOperation, exp.Subquery)
_REF_RE = re.compile(r"\b_r_\d+\b", re.IGNORECASE)
# Ways to *define* (not read) a reserved name: CTAS/view target, CTE name,
# table/derived-table alias. Reading `FROM _r_7` is the whole point and stays.
_RESERVED_DEF_RE = re.compile(
    r"(?:\btemp(?:orary)?\s+(?:table|view)\s+(?:if\s+not\s+exists\s+)?_r_\d+\b)"
    r"|(?:(?:\bwith|,)\s*(?:recursive\s+)?_r_\d+\s*(?:\([^)]*\))?\s+as\s*\()"
    r"|(?:\bas\s+_r_\d+\b)",
    re.IGNORECASE,
)


def reserved_name_misuse(sql: str) -> str | None:
    """Error text if the statement defines/aliases a framework result name."""
    if _RESERVED_DEF_RE.search(sql):
        return (
            f"names starting with {RESULT_PREFIX} are reserved for framework-materialized "
            "results — read them (FROM _r_<n>), never define or alias them"
        )
    return None


def _existing_limit(sql: str) -> tuple[bool, int | None]:
    """(parsed_ok, literal LIMIT value or None). Non-literal LIMIT → (True, -1)."""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:  # noqa: BLE001 — DuckDB syntax sqlglot cannot model; wrapper path
        return False, None
    if not isinstance(tree, _QUERY_ROOTS):
        return True, None
    lim = tree.args.get("limit")
    if lim is None:
        return True, None
    expr = lim.expression if isinstance(lim, exp.Limit) else lim
    if isinstance(expr, exp.Literal) and expr.is_int:
        return True, int(expr.this)
    return True, -1


def capped(sql: str, cap: int) -> str:
    """The query with a row cap that preserves its ORDER BY semantics.

    No LIMIT → append one (textual, no re-serialization of the model's SQL).
    LIMIT ≤ cap → unchanged. Larger/non-literal LIMIT, or SQL sqlglot cannot
    parse → wrap: DuckDB keeps a subquery's ORDER BY under an outer LIMIT
    (verified on 1.5.5, see tests).
    """
    body = sql.strip().rstrip(";").strip()
    parsed, lim = _existing_limit(body)
    if parsed and lim is None:
        return f"{body}\nLIMIT {cap}"
    if parsed and lim is not None and 0 <= lim <= cap:
        return body
    return _wrap(body, cap)


def _wrap(body: str, cap: int) -> str:
    """Outer LIMIT over a subquery — DuckDB keeps the inner ORDER BY."""
    return f"SELECT * FROM (\n{body}\n) AS _q LIMIT {cap}"  # nosec B608 - gate-checked SQL + int cap


class ResultStore:
    """Session bookkeeping for materialized result tables (LRU by last query)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._n = 0
        self._tick = 0
        self._last_used: dict[str, int] = {}

    def next_ref(self) -> str:
        self._n += 1
        return f"{RESULT_PREFIX}{self._n}"

    def refs(self) -> list[str]:
        return sorted(self._last_used, key=self._last_used.get)

    def touch(self, sql: str) -> None:
        """A statement that reads `_r_<n>` refreshes its recency."""
        for ref in {m.lower() for m in _REF_RE.findall(sql)}:
            if ref in self._last_used:
                self._tick += 1
                self._last_used[ref] = self._tick

    def register(self, ref: str, conn) -> list[str]:
        """Track a kept table; drop the least-recently-queried beyond the cap."""
        self._tick += 1
        self._last_used[ref] = self._tick
        evicted = []
        while len(self._last_used) > RESULT_TABLE_LRU:
            victim = min(self._last_used, key=self._last_used.get)
            conn.execute(f"DROP TABLE IF EXISTS {victim}")  # nosec B608 - framework-generated identifier  # nosemgrep: sqlalchemy-execute-raw-query -- framework-generated identifier
            del self._last_used[victim]
            evicted.append(victim)
        return evicted


STORE = ResultStore()


def _shorten(v):
    s = str(v)
    return s if len(s) <= SAMPLE_VALUE_CHARS else s[: SAMPLE_VALUE_CHARS - 1] + "…"


def execute(conn, governed_sql: str, query_shaped: bool, lock) -> dict:
    """Run a governed statement; return the shaped result.

    Keys: shape ('rows' | 'summary'), rows_json, n_rows, total_rows, exact,
    ref, columns, sample, engine_ms, evicted, capped (direct path only).
    Query-shaped statements (SELECT/WITH) go through materialization; metadata
    statements (DESCRIBE/SHOW/SUMMARIZE/EXPLAIN, user CTAS) take the direct
    path with a plain row cap. Everything under `lock` (invariant #6).
    """
    t0 = time.perf_counter()
    with lock:
        if not query_shaped:
            return _direct(conn, governed_sql, t0)
        ref = STORE.next_ref()
        cap = MAX_MATERIALIZE_ROWS
        ctas = f"CREATE TEMP TABLE {ref} AS\n{capped(governed_sql, cap + 1)}"
        try:
            conn.execute(ctas)  # nosemgrep: sqlalchemy-execute-raw-query -- gate-checked SQL
        except Exception as e:  # noqa: BLE001 — surface the engine error verbatim (invariant #5)
            # A textual LIMIT append can collide with rare trailing syntax; retry wrapped once.
            if "LIMIT" in str(e).upper() or "syntax" in str(e).lower():
                body = governed_sql.strip().rstrip(";")
                wrapped = f"CREATE TEMP TABLE {ref} AS\n{_wrap(body, cap + 1)}"
                conn.execute(wrapped)  # nosemgrep: sqlalchemy-execute-raw-query -- gate-checked SQL
            else:
                raise
        total = conn.execute(f"SELECT count(*) FROM {ref}").fetchone()[0]  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query -- framework-generated identifier
        exact = total <= cap
        # inline only when the result is COMPLETE and small; a capped result must
        # always arrive as a summary so the model knows it is not the whole answer
        if exact and total <= MAX_RESULT_ROWS:
            cur = conn.execute(f"SELECT * FROM {ref}")  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query -- framework-generated identifier
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            out = [dict(zip(cols, (str(v) for v in r), strict=False)) for r in rows]
            rows_json = json.dumps(out, ensure_ascii=False, default=str)
            if len(rows_json) <= MAX_RESULT_CHARS:
                conn.execute(f"DROP TABLE IF EXISTS {ref}")  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query -- framework-generated identifier
                return {
                    "shape": "rows",
                    "rows_json": rows_json,
                    "n_rows": len(out),
                    "total_rows": len(out),
                    "exact": True,
                    "ref": None,
                    "columns": [],
                    "sample": [],
                    "engine_ms": _ms(t0),
                    "evicted": [],
                    "capped": False,
                }
        # keep it: summary shape
        desc = conn.execute(f"DESCRIBE {ref}").fetchall()  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query -- framework-generated identifier
        columns = [{"name": d[0], "type": d[1]} for d in desc]
        cur = conn.execute(f"SELECT * FROM {ref} LIMIT {SAMPLE_ROWS}")  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query -- framework-generated identifier
        names = [d[0] for d in cur.description]
        sample = [dict(zip(names, (_shorten(v) for v in r), strict=False)) for r in cur.fetchall()]
        evicted = STORE.register(ref, conn)
        return {
            "shape": "summary",
            "rows_json": "",
            "n_rows": 0,
            "total_rows": int(total),
            "exact": exact,
            "ref": ref,
            "columns": columns,
            "sample": sample,
            "engine_ms": _ms(t0),
            "evicted": evicted,
            "capped": False,
        }


def _direct(conn, sql: str, t0: float) -> dict:
    cur = conn.execute(sql)  # nosemgrep: sqlalchemy-execute-raw-query -- governed SQL, gate-checked
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchmany(MAX_RESULT_ROWS + 1) if cols else []
    capped_flag = len(rows) > MAX_RESULT_ROWS
    rows = rows[:MAX_RESULT_ROWS]
    out = [dict(zip(cols, (str(v) for v in r), strict=False)) for r in rows]
    return {
        "shape": "rows",
        "rows_json": json.dumps(out, ensure_ascii=False, default=str),
        "n_rows": len(out),
        "total_rows": len(out),
        "exact": not capped_flag,
        "ref": None,
        "columns": [],
        "sample": [],
        "engine_ms": _ms(t0),
        "evicted": [],
        "capped": capped_flag,
    }


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def format_result(res: dict, governed: bool) -> str:
    """The exact string the model reads.

    Line 1: rows (JSON array) or the summary object. Line 2: metrics. Line 3
    (summary only): the caveat naming the retrieval path — every reshaped
    result says what was withheld and how to get it, without exception.
    """
    metrics = {
        "engine_ms": res["engine_ms"],
        "rows": res["total_rows"],
        "governed": governed,
        "shape": res["shape"],
    }
    if res["shape"] == "summary":
        ref = res["ref"]
        first = res["columns"][0]["name"] if res["columns"] else "*"
        summary = {
            "shape": "summary",
            "ref": ref,
            "rows": res["total_rows"],
            "rows_exact": res["exact"],
            "columns": res["columns"],
            "sample": res["sample"],
            "read_with": [
                f"SELECT * FROM {ref} LIMIT {MAX_RESULT_ROWS} OFFSET 0",  # nosec B608 - example text
                f"SELECT {first}, count(*) AS n FROM {ref} GROUP BY 1 ORDER BY n DESC LIMIT 20",  # nosec B608 - example text, not executed
            ],
        }
        metrics["ref"] = ref
        if res["evicted"]:
            metrics["evicted"] = res["evicted"]
        about = (
            f"{res['total_rows']:,} rows"
            if res["exact"]
            else f"more than {MAX_MATERIALIZE_ROWS:,} rows (first {MAX_MATERIALIZE_ROWS:,} kept)"
        )
        caveat = (
            f"[Result not shown inline: {about}. The FULL result is in session temp table "
            f"{ref} — query it with run_sql (filter, aggregate, JOIN). The {len(res['sample'])} "
            "sample rows above are a sample, not the result. To browse rows, page with "
            f"LIMIT {MAX_RESULT_ROWS} OFFSET n — results larger than that are re-materialized.]"
        )
        if res["evicted"]:
            caveat += f" [Older result tables dropped (LRU): {', '.join(res['evicted'])}]"
        return "\n".join(
            [json.dumps(summary, ensure_ascii=False, default=str), json.dumps(metrics), caveat]
        )
    payload = res["rows_json"] + "\n" + json.dumps(metrics)
    if res["capped"]:
        payload += f"\n[metadata statement capped at {MAX_RESULT_ROWS} rows]"
    return payload
