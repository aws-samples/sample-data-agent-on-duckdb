"""The read-only gate (invariant 1): which statement shapes may reach the engine.

Read statements and `CREATE TEMP TABLE|VIEW ... AS SELECT` only, one
statement per call. Session-local temp objects are the ONLY allowed writes:
they live in the in-memory db and die with the process, so they carry no
persistence risk. No persistent writes, no ATTACH from SQL, no COPY TO.
"""

from __future__ import annotations

import re

import duckdb

TEMP_DDL = re.compile(
    r"^create\s+(?:or\s+replace\s+)?temp(?:orary)?\s+(?:table|view)\s", re.IGNORECASE
)
READ_PREFIXES = ("select", "describe", "summarize", "with", "show", "explain")


def single_statement(sql: str) -> bool:
    """True if `sql` is exactly one statement.

    The gate matches on the leading keyword, so without this check a write
    rides in behind a read: `SELECT 1; DROP TABLE t` starts with "select",
    passes, and DuckDB's execute() runs BOTH statements. Verified: the table
    was dropped, and `SELECT 1; COPY t TO '/tmp/f.csv'` wrote the file — the
    exfiltration primitive the gate exists to block.

    Splitting is delegated to DuckDB's own parser rather than a hand-rolled
    scan for ';'. A textual scan has to know about single/double quotes, line
    and block comments and dollar-quoting to avoid both false positives
    (`WHERE txid = 'a;b'`) and false negatives, and any divergence from the
    engine's lexer is exactly the gap an attacker aims at. extract_statements
    uses the same parser that will execute the SQL, so there is nothing to
    diverge. Unparseable SQL is left to the engine, which is where the model's
    self-repair loop expects the error to come from.
    """
    try:
        return len(duckdb.extract_statements(sql)) <= 1
    except Exception:  # noqa: BLE001 — unparseable: DuckDB will reject it too
        return True


def strip_leading_comments(sql: str) -> str:
    """Drop leading -- line and /* */ block comments so the gate classifies on
    the first real keyword. Models routinely prefix a query with a comment;
    stripping cannot weaken the gate (a commented-out DROP still starts with
    DROP once the comment is removed, and the single-statement check runs on
    the ORIGINAL text)."""
    s = sql.lstrip()
    while True:
        if s.startswith("--"):
            nl = s.find("\n")
            s = "" if nl == -1 else s[nl + 1 :]
        elif s.startswith("/*"):
            end = s.find("*/")
            s = "" if end == -1 else s[end + 2 :]
        else:
            return s
        s = s.lstrip()


def normalize(sql: str) -> str:
    """Comment-stripped, lower-cased, whitespace-collapsed — the gate's view."""
    return " ".join(strip_leading_comments(sql).lower().split())


def check(sql: str) -> str | None:
    """Return an error string if the statement is not allowed, else None."""
    if not single_statement(sql):
        return (
            "ERROR: submit one statement per call — multi-statement SQL is not "
            "allowed (the read-only gate applies to the leading statement only)."
        )
    normalized = normalize(sql)
    if normalized.startswith(READ_PREFIXES) or TEMP_DDL.match(normalized):
        return None
    return (
        "ERROR: only SELECT / DESCRIBE / SUMMARIZE / WITH / SHOW / EXPLAIN or "
        "CREATE TEMP TABLE|VIEW ... AS SELECT are allowed."
    )


def query_shaped(sql: str) -> bool:
    """SELECT/WITH statements are materialized-and-shaped; metadata statements
    (DESCRIBE/SHOW/SUMMARIZE/EXPLAIN) and user CTAS take the direct path."""
    return strip_leading_comments(sql).lstrip().lower().startswith(("select", "with"))
