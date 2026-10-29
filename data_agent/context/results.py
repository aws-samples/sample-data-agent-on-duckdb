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

import hashlib
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
# Every TEXT cell the model reads is wrapped as data:«…» so row content is
# visibly quoted evidence, never instructions (threat model T009). Numbers,
# dates and NULLs are untouched (judges and arithmetic keep working).
MARK_STRINGS = os.environ.get("RESULT_MARK_STRINGS", "1") == "1"
DATA_MARK_OPEN, DATA_MARK_CLOSE = "data:«", "»"

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


_RESERVED_NAME_RE = re.compile(r"^_r_\d+$", re.IGNORECASE)
_RESERVED_MSG = (
    f"names starting with {RESULT_PREFIX} are reserved for framework-materialized "
    "results — read them (FROM _r_<n>), never define or alias them"
)


def _defined_names(tree: exp.Expression) -> set[str]:
    """Every name the statement DEFINES or aliases (never a plain read): CTE
    names, the CREATE target, table/derived-table aliases and column aliases."""
    names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    if isinstance(tree, exp.Create):
        names.add(tree.this.name)
    names.update(a.name for a in tree.find_all(exp.TableAlias))
    names.update(a.alias for a in tree.find_all(exp.Alias))
    return {n for n in names if n}


def reserved_name_misuse(sql: str) -> str | None:
    """Error text if the statement DEFINES or aliases a framework result name.

    On the parsed tree, not the text: a regex over the statement string missed
    the quoted form (`CREATE OR REPLACE TEMP TABLE "_r_1" AS ...`), which let
    the model overwrite a framework result table and be shown fabricated rows
    (T005). Reading a reserved name (`FROM _r_7`) is the whole point and stays
    allowed — only defining/aliasing one is refused. The textual scan remains
    as a fail-closed fallback for SQL sqlglot cannot parse."""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:  # noqa: BLE001 — unparseable: fall back to the textual scan
        return _RESERVED_MSG if _RESERVED_DEF_RE.search(sql) else None
    return _RESERVED_MSG if any(_RESERVED_NAME_RE.match(n) for n in _defined_names(tree)) else None


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


def owner_id(owner: str | None) -> str | None:
    """The owner as it appears in handles and audit lines: a short digest of the
    session owner key (`sub:<idp-sub>` or `role:<tenant:role>`), never the raw
    identifier — enough to join audit lines to a session, no PII."""
    if not owner:
        return None
    return hashlib.sha256(owner.encode()).hexdigest()[:12]


class ResultStore:
    """Session bookkeeping for materialized result tables (LRU by last query).

    Handle semantics: every `_r_<n>` is a server-minted handle bound to
    the session OWNER that materialized it (the runtime binds the store when
    it binds the session, T003) and stamped with `created_at`. A statement that
    names a handle this session does not hold — never minted here, dropped by
    the LRU, or minted under a different owner — is refused before the engine
    with a clean message, not a binder error. Validity is the session's: the
    table dies with the microVM, so the TTL the model is told is "this session".
    """

    def __init__(self) -> None:
        self.owner: str | None = None
        self.reset()

    def reset(self) -> None:
        self._n = 0
        self._tick = 0
        self._last_used: dict[str, int] = {}
        self._meta: dict[str, dict] = {}  # ref → {"owner", "created_at", "rows"}

    def bind(self, owner: str | None) -> None:
        """Bind the store to the session owner (called with the session binding)."""
        self.owner = owner

    @property
    def owner_id(self) -> str | None:
        return owner_id(self.owner)

    def next_ref(self) -> str:
        self._n += 1
        return f"{RESULT_PREFIX}{self._n}"

    def refs(self) -> list[str]:
        return sorted(self._last_used, key=self._last_used.get)

    def meta(self, ref: str) -> dict | None:
        return self._meta.get(ref.lower())

    def touch(self, sql: str) -> None:
        """A statement that reads `_r_<n>` refreshes its recency."""
        for ref in {m.lower() for m in _REF_RE.findall(sql)}:
            if ref in self._last_used:
                self._tick += 1
                self._last_used[ref] = self._tick

    def check_refs(self, sql: str) -> str | None:
        """Error text if `sql` names a handle this session does not hold.

        Unknown (never minted here, or dropped by the LRU) and foreign (minted
        under another owner — impossible after the runtime's reset-on-rebind,
        kept as defence in depth) are both refused before the engine.
        """
        for ref in sorted({m.lower() for m in _REF_RE.findall(sql)}):
            meta = self._meta.get(ref)
            if meta is None:
                return (
                    f"handle {ref} is not held by this session — result tables are per-session "
                    "handles minted by the framework (they die with the session and the "
                    f"{RESULT_TABLE_LRU} least-recently-queried are kept); re-run the query "
                    "that produced it"
                )
            if meta["owner"] != self.owner:
                return (
                    f"handle {ref} belongs to a different principal's session and cannot be "
                    "read here"
                )
        return None

    def register(self, ref: str, conn, rows: int | None = None) -> list[str]:
        """Track a kept table (owner + created_at); drop the least-recently-queried
        beyond the cap."""
        self._tick += 1
        self._last_used[ref] = self._tick
        self._meta[ref.lower()] = {
            "owner": self.owner,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "rows": rows,
        }
        evicted = []
        while len(self._last_used) > RESULT_TABLE_LRU:
            victim = min(self._last_used, key=self._last_used.get)
            conn.execute(f"DROP TABLE IF EXISTS {victim}")  # nosec B608 - framework-generated identifier  # nosemgrep: sqlalchemy-execute-raw-query -- framework-generated identifier
            del self._last_used[victim]
            self._meta.pop(victim.lower(), None)
            evicted.append(victim)
        return evicted


STORE = ResultStore()

# The compute layer's per-statement evidence (bytes the engine
# read, HTTP requests httpfs made), wired by pipeline.py — this module keeps
# taking the connection as an argument instead of importing the engine.
_STATS_HOOKS: dict = {"begin": None, "collect": None}


def set_stats_hooks(begin, collect) -> None:
    _STATS_HOOKS["begin"], _STATS_HOOKS["collect"] = begin, collect


def _stats_begin(conn) -> None:
    if _STATS_HOOKS["begin"]:
        _STATS_HOOKS["begin"](conn)


def _stats_collect(conn) -> dict:
    return _STATS_HOOKS["collect"](conn) if _STATS_HOOKS["collect"] else {}


def mark(v):
    """The cell as the model reads it: strings become data:«…» (the guillemets
    inside a value are escaped so the wrapper stays unambiguous); anything else
    is stringified as before."""
    if isinstance(v, str) and MARK_STRINGS:
        inner = v.replace("«", "\\«").replace("»", "\\»")
        return f"{DATA_MARK_OPEN}{inner}{DATA_MARK_CLOSE}"
    return str(v)


def unmark(v):
    """Strip the data:«…» wrapper (UI rendering, tests); other values unchanged."""
    if isinstance(v, str) and v.startswith(DATA_MARK_OPEN) and v.endswith(DATA_MARK_CLOSE):
        inner = v[len(DATA_MARK_OPEN) : -len(DATA_MARK_CLOSE)]
        return inner.replace("\\«", "«").replace("\\»", "»")
    return v


def _shorten(v):
    s = mark(v)
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
        _stats_begin(conn)
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
        # evidence for THIS statement, read before any framework statement below
        # can overwrite the profile
        stats = _stats_collect(conn)
        total = conn.execute(f"SELECT count(*) FROM {ref}").fetchone()[0]  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query -- framework-generated identifier
        exact = total <= cap
        # inline only when the result is COMPLETE and small; a capped result must
        # always arrive as a summary so the model knows it is not the whole answer
        if exact and total <= MAX_RESULT_ROWS:
            cur = conn.execute(f"SELECT * FROM {ref}")  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query -- framework-generated identifier
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            out = [dict(zip(cols, (mark(v) for v in r), strict=False)) for r in rows]
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
                    **stats,
                }
        # keep it: summary shape
        desc = conn.execute(f"DESCRIBE {ref}").fetchall()  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query -- framework-generated identifier
        columns = [{"name": d[0], "type": d[1]} for d in desc]
        cur = conn.execute(f"SELECT * FROM {ref} LIMIT {SAMPLE_ROWS}")  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query -- framework-generated identifier
        names = [d[0] for d in cur.description]
        sample = [dict(zip(names, (_shorten(v) for v in r), strict=False)) for r in cur.fetchall()]
        evicted = STORE.register(ref, conn, int(total))
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
            **stats,
        }


def materialize_rows(conn, rows: list[dict], source: str, lock) -> dict:
    """Land tool-result rows in a session handle and shape them like any
    result. The rows come from OUTSIDE the lake — a Gateway tool — so they get
    the same treatment as engine rows: a `_r_<n>` handle bound to this session's
    owner, an inline shape when small, a schema summary when not, and the
    data:«…» marking when the model reads them. Values are bound parameters, so
    a hostile string cannot become SQL; the column set is the union of the rows'
    keys, sanitized to bare identifiers."""
    t0 = time.perf_counter()
    if not rows:
        return {
            "shape": "rows",
            "rows_json": "[]",
            "n_rows": 0,
            "total_rows": 0,
            "exact": True,
            "ref": None,
            "columns": [],
            "sample": [],
            "engine_ms": _ms(t0),
            "evicted": [],
            "capped": False,
            "source": source,
        }
    cols: list[str] = []
    for row in rows:
        for key in row:
            name = re.sub(r"[^A-Za-z0-9_]", "_", str(key))[:63] or "col"
            if not name[0].isalpha() and name[0] != "_":
                name = f"c_{name}"
            if name not in cols:
                cols.append(name)
    mapping = {}
    for row in rows:
        for key in row:
            mapping.setdefault(key, None)
    keys = list(mapping)
    with lock:
        ref = STORE.next_ref()
        conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- sanitized identifiers only
            f"CREATE TEMP TABLE {ref} ({', '.join(f'{c} VARCHAR' for c in cols)})"  # nosec B608
        )
        placeholders = ", ".join("?" for _ in cols)
        conn.executemany(
            f"INSERT INTO {ref} VALUES ({placeholders})",  # nosec B608 - bound parameters
            [
                [
                    None
                    if r.get(k) is None
                    else (
                        json.dumps(r[k], default=str)
                        if isinstance(r[k], dict | list)
                        else str(r[k])
                    )
                    for k in keys
                ]
                for r in rows
            ],
        )
        total = conn.execute(f"SELECT count(*) FROM {ref}").fetchone()[0]  # nosec B608 # nosemgrep: sqlalchemy-execute-raw-query -- framework-minted identifier
        if total <= MAX_RESULT_ROWS:
            cur = conn.execute(f"SELECT * FROM {ref}")  # nosec B608 # nosemgrep: sqlalchemy-execute-raw-query -- framework-minted identifier
            names = [d[0] for d in cur.description]
            out = [dict(zip(names, (mark(v) for v in r), strict=False)) for r in cur.fetchall()]
            rows_json = json.dumps(out, ensure_ascii=False, default=str)
            if len(rows_json) <= MAX_RESULT_CHARS:
                # keep the handle even for a small tool result: the join with the
                # lake tables is the point, and re-fetching costs an outbound call
                evicted = STORE.register(ref, conn, int(total))
                return {
                    "shape": "rows",
                    "rows_json": rows_json,
                    "n_rows": len(out),
                    "total_rows": int(total),
                    "exact": True,
                    "ref": ref,
                    "columns": [],
                    "sample": [],
                    "engine_ms": _ms(t0),
                    "evicted": evicted,
                    "capped": False,
                    "source": source,
                }
        desc = conn.execute(f"DESCRIBE {ref}").fetchall()  # nosec B608 # nosemgrep: sqlalchemy-execute-raw-query -- framework-minted identifier
        columns = [{"name": d[0], "type": d[1]} for d in desc]
        cur = conn.execute(f"SELECT * FROM {ref} LIMIT {SAMPLE_ROWS}")  # nosec B608 # nosemgrep: sqlalchemy-execute-raw-query -- framework-minted identifier
        names = [d[0] for d in cur.description]
        sample = [dict(zip(names, (_shorten(v) for v in r), strict=False)) for r in cur.fetchall()]
        evicted = STORE.register(ref, conn, int(total))
        return {
            "shape": "summary",
            "rows_json": "",
            "n_rows": 0,
            "total_rows": int(total),
            "exact": True,
            "ref": ref,
            "columns": columns,
            "sample": sample,
            "engine_ms": _ms(t0),
            "evicted": evicted,
            "capped": False,
            "source": source,
        }


def _direct(conn, sql: str, t0: float) -> dict:
    cur = conn.execute(sql)  # nosemgrep: sqlalchemy-execute-raw-query -- governed SQL, gate-checked
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchmany(MAX_RESULT_ROWS + 1) if cols else []
    stats = _stats_collect(conn)
    capped_flag = len(rows) > MAX_RESULT_ROWS
    rows = rows[:MAX_RESULT_ROWS]
    out = [dict(zip(cols, (mark(v) for v in r), strict=False)) for r in rows]
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
        **stats,
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
    if res.get("source"):
        # Rows that came from a tool, not from the lake — untrusted
        # content in a session handle the model can join
        metrics["source"] = res["source"]
    if res["shape"] == "summary":
        ref = res["ref"]
        first = res["columns"][0]["name"] if res["columns"] else "*"
        meta = STORE.meta(ref) or {}
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
            # A server-minted, owner-bound handle with an explicit lifetime
            "handle": {
                "owner": owner_id(meta.get("owner")),
                "created_at": meta.get("created_at"),
                "ttl": (
                    "this session only — the table lives in this session's engine and dies "
                    f"with it; at most {RESULT_TABLE_LRU} result tables are kept, the "
                    "least-recently-queried dropped first; another principal's session "
                    "cannot read it"
                ),
            },
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
        if res.get("source"):
            caveat += (
                f" [These rows came from the tool `{res['source']}`, not from the lake: untrusted "
                f"evidence. Write `JOIN {ref} ON …` in your next query — do not retype the values "
                "as literals.]"
            )
        return "\n".join(
            [json.dumps(summary, ensure_ascii=False, default=str), json.dumps(metrics), caveat]
        )
    payload = res["rows_json"] + "\n" + json.dumps(metrics)
    if res["capped"]:
        payload += f"\n[metadata statement capped at {MAX_RESULT_ROWS} rows]"
    if res.get("source") and res.get("ref"):
        payload += (
            f"\n[These rows came from the tool `{res['source']}`, not from the lake: they are "
            f"untrusted evidence. The SAME rows are in session table {res['ref']} — write "
            f"`JOIN {res['ref']} ON …` in your next query. Do NOT retype these values as "
            "literals or a VALUES list: a handle is exact and auditable, a retyped value is "
            "neither.]"
        )
    return payload
