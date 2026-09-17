#!/usr/bin/env python3
"""Pre-install the DuckDB core extensions the engine LOADs.

The runtime never runs `INSTALL` (threat model T010: a cold-start download from
the extension repository is an avoidable standing supply-chain dependency and
a cold-start cost). This script is the ONE place extensions are fetched: the
container image runs it at build time into `DUCKDB_EXTENSION_DIR`; a developer
or the eval worker runs it once per DuckDB version.

  python scripts/install_extensions.py [--dir /opt/duckdb-extensions] [--extra postgres]
"""

from __future__ import annotations

import argparse
import os
import sys

import duckdb

CORE = ("httpfs", "aws", "iceberg", "ducklake", "fts")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dir",
        default=os.environ.get("DUCKDB_EXTENSION_DIR", ""),
        help="extension directory (default: DuckDB's, under the home directory)",
    )
    ap.add_argument("--extra", nargs="*", default=[], help="additional extensions")
    args = ap.parse_args()
    c = duckdb.connect()
    c.execute("SET home_directory='/tmp'")  # nosec B108 - matches engine._configure
    if args.dir:
        c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- operator arg
            f"SET extension_directory='{args.dir}'"
        )
    for ext in (*CORE, *args.extra):
        c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- fixed extension names
            f"INSTALL {ext}"
        )
        c.execute(f"LOAD {ext}")  # nosemgrep: sqlalchemy-execute-raw-query -- fixed extension names
    rows = c.execute(
        "SELECT extension_name, extension_version, install_path FROM duckdb_extensions() "
        "WHERE installed ORDER BY 1"
    ).fetchall()
    print(f"duckdb {duckdb.__version__}; installed:")
    for name, ver, path in rows:
        print(f"  {name:10} {ver or '':12} {path}")
    missing = [e for e in (*CORE, *args.extra) if e not in {r[0] for r in rows}]
    if missing:
        sys.exit(f"not installed: {missing}")


if __name__ == "__main__":
    main()
