#!/usr/bin/env python3
"""Load the sample dataset into S3 Tables, DuckLake, and the label tables —
using DuckDB itself.

No Glue job, no Spark cluster, no Athena CTAS: DuckDB writes the Iceberg
table through the S3 Tables REST catalog, builds the DuckLake catalog
directly, and materializes the attribution label tables as Parquet. The
loader doubles as a working example: the same in-process engine the agent
queries with is also the ingestion tool.

The slice is the incident-window `eth_traces` internal calls (value > 0),
including from_address/to_address so the tracing and governance stories are
meaningful. Run this on AWS-side compute (the AgentCore region) — reading the
public us-east-2 dataset over the AWS backbone is far faster than from a
laptop.

Usage (after `cdk deploy`, with your own credentials — not the runtime role):
  python scripts/load_s3tables.py \
      --table-bucket-arn arn:aws:s3tables:us-west-2:<acct>:bucket/data-agent-tables \
      --ducklake s3://duckdb-agent-ducklake-<acct>/catalog/blockchain.ducklake \
      --labels-prefix s3://duckdb-agent-ducklake-<acct>/labels \
      --days 2025-02-21:2025-02-23

Idempotent: CREATE TABLE IF NOT EXISTS + full re-load is skipped when the
row count already matches the source slice.
"""

import argparse
import json
import sys
import urllib.request

import duckdb

SRC = "s3://aws-public-blockchain/v1.0/eth/traces"
# internal-call slice with address columns — the tracing/governance surface
COLS = (
    "block_number, transaction_hash, from_address, to_address, value, "
    "call_type, trace_type, status, block_timestamp, CAST(date AS VARCHAR) AS date"
)

# REAL public OFAC-sanctioned ETH address list (MIT-licensed community mirror
# of the OFAC SDN crypto entries). Cited in docs; not synthetic.
OFAC_ETH_URL = (
    "https://raw.githubusercontent.com/0xB10C/"
    "ofac-sanctioned-digital-currency-addresses/lists/sanctioned_addresses_ETH.json"
)

# SYNTHETIC exchange deposit-address labels — the entity attribution here
# (exchange, label, subject_identity, risk_tier) is ILLUSTRATIVE, NOT real.
# The subject_identity column exists purely to demonstrate column-level
# governance (a junior role must not see it).
#
# The first two addresses ARE real first-hop recipients of the publicly
# reported incident fan-out (each received 10,000 ETH). We attach synthetic
# exchange-a labels to them so the demo's step-7 attribution join produces a
# real cash-out hit; the labels themselves remain synthetic. The rest are
# fully synthetic placeholder addresses.
SYNTHETIC_CEX_LABELS = [
    (
        "0x36ed3c0213565530c35115d93a80f9c04d94e4cb",
        "exchange-a",
        "deposit",
        "customer#A-1042",
        "high",
    ),
    (
        "0xaf620e6d32b1c67f3396ef5d2f7d7642dc2e6ce9",
        "exchange-a",
        "deposit",
        "customer#A-2288",
        "high",
    ),
    (
        "0x28c6c06298d514db089934071355e5743bf21d60",
        "exchange-a",
        "hot_wallet",
        "ACME-CEX Treasury",
        "low",
    ),
    (
        "0x56eddb7aa87536c09ccc2793473599fd21a8b17f",
        "exchange-a",
        "deposit",
        "customer#A-3391",
        "high",
    ),
    (
        "0x9696f59e4d72e237be84ffd425dcad154bf96976",
        "exchange-b",
        "deposit",
        "customer#B-0007",
        "medium",
    ),
    (
        "0x4976a4a02f38326660d17bf34b431dc6e2eb2327",
        "exchange-b",
        "deposit",
        "customer#B-0910",
        "low",
    ),
    (
        "0xd551234ae421e3bcba99a0da6d736074f22192ff",
        "exchange-b",
        "hot_wallet",
        "BetaEx Treasury",
        "low",
    ),
    (
        "0x564286362092d8e7936f0549571a803b203aaced",
        "exchange-c",
        "deposit",
        "customer#C-5501",
        "high",
    ),
    (
        "0x0681d8db095565fe8a346fa0277bffde9c0edbbf",
        "exchange-c",
        "deposit",
        "customer#C-6620",
        "medium",
    ),
    (
        "0xfe9e8709d3215310075d67e3ed32a380ccf451c8",
        "exchange-c",
        "deposit",
        "customer#C-7788",
        "low",
    ),
    (
        "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be",
        "exchange-d",
        "hot_wallet",
        "DeltaEx Treasury",
        "low",
    ),
    (
        "0xa090e606e30bd747d4e6245a1517ebe430f0057e",
        "exchange-d",
        "deposit",
        "customer#D-1200",
        "medium",
    ),
]


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
        f"SELECT {COLS} FROM read_parquet('{SRC}/*/*.parquet', hive_partitioning=1) "  # nosec B608 - operator-run loader; COLS/SRC are constants, days is the operator's own arg
        f"WHERE date BETWEEN '{start}' AND '{end}' AND value > 0"
    )


def load_s3tables(c: duckdb.DuckDBPyConnection, arn: str, days: str) -> int:
    # SQL interpolates only this operator-run CLI's own args, not user input.
    c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- CLI args
        f"ATTACH '{arn}' AS s3t (TYPE iceberg, ENDPOINT_TYPE s3_tables)"
    )
    c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- CLI args
        f"CREATE TABLE IF NOT EXISTS s3t.blockchain.eth_traces AS {source_select(days)}"
    )
    return c.execute("SELECT count(*) FROM s3t.blockchain.eth_traces").fetchone()[0]


def load_ducklake(c: duckdb.DuckDBPyConnection, catalog: str, days: str) -> int:
    data_path = catalog.rsplit("/catalog/", 1)[0] + "/data"
    c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- CLI args
        f"ATTACH 'ducklake:{catalog}' AS dl (DATA_PATH '{data_path}')"
    )
    c.execute("CREATE SCHEMA IF NOT EXISTS dl.blockchain")
    # copy from the just-loaded Iceberg table (fast, same region) if present,
    # else from the public source
    has_s3t = c.execute(
        "SELECT count(*) FROM duckdb_databases() WHERE database_name='s3t'"
    ).fetchone()[0]
    src = "SELECT * FROM s3t.blockchain.eth_traces" if has_s3t else source_select(days)
    c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- CLI args
        f"CREATE TABLE IF NOT EXISTS dl.blockchain.eth_traces AS {src}"
    )
    return c.execute("SELECT count(*) FROM dl.blockchain.eth_traces").fetchone()[0]


def load_labels(c: duckdb.DuckDBPyConnection, prefix: str) -> tuple[int, int]:
    """Write ofac_addresses (real, fetched) + cex_deposit_labels (synthetic)
    as single Parquet files under the prefix."""
    base = prefix.rstrip("/")
    with urllib.request.urlopen(OFAC_ETH_URL, timeout=30) as r:  # noqa: S310  # nosec B310 - fixed https URL constant  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        ofac = json.load(r)
    ofac_src = "github.com/0xB10C/ofac-sanctioned-digital-currency-addresses"
    c.execute(
        "CREATE OR REPLACE TABLE ofac_addresses (address VARCHAR, program VARCHAR, source VARCHAR)"
    )
    c.executemany(
        "INSERT INTO ofac_addresses VALUES (?, 'OFAC-SDN', ?)",
        [(a.lower(), ofac_src) for a in ofac],
    )
    c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- CLI args, not user input
        f"COPY (SELECT * FROM ofac_addresses) TO '{base}/ofac_addresses.parquet' (FORMAT parquet)"  # nosec B608 - operator-run loader; base is the operator's own --labels-prefix arg
    )
    n_ofac = c.execute("SELECT count(*) FROM ofac_addresses").fetchone()[0]

    c.execute(
        "CREATE OR REPLACE TABLE cex_deposit_labels (address VARCHAR, exchange VARCHAR, "
        "label VARCHAR, subject_identity VARCHAR, risk_tier VARCHAR)"
    )
    c.executemany(
        "INSERT INTO cex_deposit_labels VALUES (?, ?, ?, ?, ?)",
        [(a.lower(), ex, lb, si, rt) for (a, ex, lb, si, rt) in SYNTHETIC_CEX_LABELS],
    )
    c.execute(
        f"COPY (SELECT * FROM cex_deposit_labels) "
        f"TO '{base}/cex_deposit_labels.parquet' (FORMAT parquet)"  # nosec B608 - operator-run loader; base is the operator's own --labels-prefix arg
    )
    n_cex = c.execute("SELECT count(*) FROM cex_deposit_labels").fetchone()[0]
    return n_ofac, n_cex


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table-bucket-arn", default="", help="S3 Tables bucket ARN (Path B)")
    ap.add_argument("--ducklake", default="", help="DuckLake catalog URI (optional, Path D)")
    ap.add_argument("--labels-prefix", default="", help="s3:// prefix for label tables")
    ap.add_argument("--days", default="2025-02-21:2025-02-23", help="date range start:end")
    args = ap.parse_args()

    c = connect()
    if args.table_bucket_arn:
        n = load_s3tables(c, args.table_bucket_arn, args.days)
        print(f"s3t.blockchain.eth_traces: {n:,} rows")
    if args.ducklake:
        n = load_ducklake(c, args.ducklake, args.days)
        print(f"dl.blockchain.eth_traces: {n:,} rows")
    if args.labels_prefix:
        n_ofac, n_cex = load_labels(c, args.labels_prefix)
        print(
            f"ofac_addresses: {n_ofac} rows (real) · cex_deposit_labels: {n_cex} rows (synthetic)"
        )
    print("load complete")


if __name__ == "__main__":
    sys.exit(main())
