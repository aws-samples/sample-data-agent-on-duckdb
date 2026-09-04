"""Universal hardening — rules for EVERY principal, the
unrestricted role included. Each closes a bypass that was verified live:

  * `WITH x AS (...) INSERT INTO t ...` / `... DELETE ...`: the read-only gate
    classifies on the leading keyword ("with"), the DML root executed.
  * `read_text('/etc/hosts')`, `glob('/etc/*')`, `duckdb_secrets()`: table
    functions that reach the container filesystem / secret store — plain
    "reads" the raw-path allowance never meant to include.
  * `FROM '/etc/passwd'` / `FROM 'https://…'`: DuckDB's replacement scan
    turns a string literal into a file read; sqlglot parses it as an
    ordinary Identifier, so the Identifier-only allowlist saw a table name.
Paths must carry an allowed scheme (RAW_PATH_SCHEMES, default s3://).
"""

from __future__ import annotations

import os
import re

from sqlglot import exp, parse_one

_DENIED_ROOTS = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Copy,
    exp.Pragma,
    exp.Set,
    exp.Attach,
)
_BANNED_FUNCS = frozenset(
    {"duckdb_secrets", "which_secret", "read_text", "read_blob", "glob", "getenv"}
)
_BANNED_FUNC_RE = re.compile(r"\b(" + "|".join(sorted(_BANNED_FUNCS)) + r")\s*\(", re.IGNORECASE)
RAW_PATH_SCHEMES = tuple(
    s.strip() for s in os.environ.get("RAW_PATH_SCHEMES", "s3://").split(",") if s.strip()
)
# a table name / string argument that DuckDB would treat as a file path
_PATH_LIKE_RE = re.compile(
    r"[/\\]|://|\.(parquet|csv|tsv|json|ndjson|jsonl|gz|zst|txt|db|duckdb)$|[*?\[]",
    re.IGNORECASE,
)


def is_path_ref(tbl: exp.Table) -> bool:
    """`FROM 'some/path.parquet'` — an Identifier that is really a file path."""
    return (
        isinstance(tbl.this, exp.Identifier)
        and not tbl.db
        and not tbl.catalog
        and bool(_PATH_LIKE_RE.search(tbl.name))
    )


def check_path(path: str) -> None:
    if not path.lower().startswith(tuple(s.lower() for s in RAW_PATH_SCHEMES)):
        raise ValueError(
            f"governance: file path {path!r} is not under an allowed scheme "
            f"{list(RAW_PATH_SCHEMES)} — local filesystem and http(s) sources are refused"
        )


def universal_checks(sql: str):
    """Rules for every principal. Returns the parsed tree, or None when sqlglot
    cannot parse (the keyword-level function ban still applied)."""
    m = _BANNED_FUNC_RE.search(sql)
    if m:
        raise ValueError(
            f"governance: function {m.group(1)!r} is not allowed — it reaches the "
            "engine host's filesystem, environment or secret store"
        )
    try:
        tree = parse_one(sql, read="duckdb")
    except Exception:  # noqa: BLE001 — caller decides (restricted: fail closed)
        return None
    if isinstance(tree, _DENIED_ROOTS):
        raise ValueError(
            f"governance: statement type {type(tree).__name__.upper()!r} is not allowed "
            "(read statements and CREATE TEMP TABLE|VIEW only, with or without a CTE prefix)"
        )
    for fn in tree.find_all(exp.Anonymous):
        if fn.name.lower() in _BANNED_FUNCS:
            raise ValueError(
                f"governance: function {fn.name!r} is not allowed — it reaches the "
                "engine host's filesystem, environment or secret store"
            )
    for tbl in tree.find_all(exp.Table):
        if is_path_ref(tbl):
            check_path(tbl.name)
        elif not isinstance(tbl.this, exp.Identifier):
            for lit in tbl.this.find_all(exp.Literal):
                if lit.is_string and _PATH_LIKE_RE.search(lit.this):
                    check_path(lit.this)
    return tree
