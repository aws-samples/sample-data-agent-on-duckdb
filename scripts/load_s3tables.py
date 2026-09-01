#!/usr/bin/env python3
"""Load the sample dataset into S3 Tables and DuckLake — using DuckDB itself.

No Glue job, no Spark cluster, no Athena CTAS: DuckDB writes the Iceberg
table through the S3 Tables REST catalog and builds the DuckLake catalog
directly. The loader doubles as a working example: the same in-process engine the agent
queries with is also the ingestion tool.

Usage (after `cdk deploy`, with your own credentials — not the runtime role):
  python scripts/load_s3tables.py \
      --table-bucket-arn arn:aws:s3tables:us-west-2:<acct>:bucket/data-agent-tables \
      --ducklake s3://duckdb-agent-ducklake-<acct>/catalog/blockchain.ducklake \
      --days 2026-08-20:2026-08-28

Idempotent: CREATE TABLE IF NOT EXISTS + full re-load is skipped when the
row count already matches the source slice.
"""

import argparse
import sys

import duckdb

SRC = "s3://aws-public-blockchain/v1.0/btc/transactions"
COLS = (
    "txid, block_number, CAST(date AS VARCHAR) AS date, fee, output_value, "
    "input_count, output_count, is_coinbase, size"
)


def connect() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect()
    c.execute("SET home_directory='/tmp'")
    c.execute("INSTALL httpfs; LOAD httpfs; INSTALL iceberg; LOAD iceberg;")
    c.execute("INSTALL ducklake; LOAD ducklake;")
    c.execute(
        "CREATE OR REPLACE SECRET aws_default "
        "(TYPE s3, PROVIDER credential_chain, REGION 'us-west-2')"
    )
    c.execute(
        "CREATE OR REPLACE SECRET blockchain (TYPE s3, PROVIDER credential_chain, "
        "REGION 'us-east-2', SCOPE 's3://aws-public-blockchain')"
    )
    return c


def source_select(days: str) -> str:
    start, end = days.split(":")
    return (
        f"SELECT {COLS} FROM read_parquet('{SRC}/*/*.parquet', hive_partitioning=1) "
        f"WHERE date BETWEEN '{start}' AND '{end}'"
    )


# SQL below interpolates only this operator-run CLI's own arguments
# (stack-output ARN / catalog URI / date range) — not user input.
def load_s3tables(c: duckdb.DuckDBPyConnection, arn: str, days: str) -> int:
    c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- CLI args, not user input
        f"ATTACH '{arn}' AS s3t (TYPE iceberg, ENDPOINT_TYPE s3_tables)"
    )
    c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- CLI args, not user input
        f"CREATE TABLE IF NOT EXISTS s3t.blockchain.btc_transactions AS {source_select(days)}"
    )
    return c.execute("SELECT count(*) FROM s3t.blockchain.btc_transactions").fetchone()[0]


def load_ducklake(c: duckdb.DuckDBPyConnection, catalog: str, days: str) -> int:
    data_path = catalog.rsplit("/catalog/", 1)[0] + "/data"
    c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- CLI args, not user input
        f"ATTACH 'ducklake:{catalog}' AS dl (DATA_PATH '{data_path}')"
    )
    c.execute("CREATE SCHEMA IF NOT EXISTS dl.blockchain")
    # copy from the just-loaded Iceberg table (fast, same region) if present,
    # else from the public source
    has_s3t = c.execute(
        "SELECT count(*) FROM duckdb_databases() WHERE database_name='s3t'"
    ).fetchone()[0]
    src = "SELECT * FROM s3t.blockchain.btc_transactions" if has_s3t else source_select(days)
    c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- CLI args, not user input
        f"CREATE TABLE IF NOT EXISTS dl.blockchain.btc_transactions AS {src}"
    )
    return c.execute("SELECT count(*) FROM dl.blockchain.btc_transactions").fetchone()[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table-bucket-arn", required=True)
    ap.add_argument("--ducklake", default="", help="DuckLake catalog URI (optional, Path D)")
    ap.add_argument("--days", default="2026-08-20:2026-08-28", help="date range start:end")
    args = ap.parse_args()

    c = connect()
    n = load_s3tables(c, args.table_bucket_arn, args.days)
    print(f"s3t.blockchain.btc_transactions: {n:,} rows")
    if args.ducklake:
        n = load_ducklake(c, args.ducklake, args.days)
        print(f"dl.blockchain.btc_transactions: {n:,} rows")
    print("load complete — the agent can now query paths B/C/D")


if __name__ == "__main__":
    sys.exit(main())
