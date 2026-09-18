#!/usr/bin/env python3
"""Load the cloud-ops CUR month into Amazon Redshift Serverless.

Stages the curated Parquet month as ONE file under <solution bucket>/ops/redshift/,
COPYs it into `ops.cur_line_items` through the Data API, grants the source
Lambda's IAM-mapped Redshift user USAGE/SELECT on the schema, and prints the row
count and total so they can be compared with the lake (they matched exactly on
2026-09-10: 1,034,397 rows / 540.80 USD). Run on AWS-side compute.

  REDSHIFT_ROLE_ARN=<DataAgentRedshiftRole arn> SOLUTION_BUCKET=<bucket> \
      python scripts/redshift_load.py --period 2026-08
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import boto3

REGION = os.environ.get("AGENT_REGION", "us-west-2")
rd = boto3.client("redshift-data", region_name=REGION)
WG = os.environ.get("REDSHIFT_WORKGROUP", "data-agent")
DB = os.environ.get("REDSHIFT_DATABASE", "dev")
ROLE = os.environ.get("REDSHIFT_ROLE_ARN", "")
BUCKET = os.environ.get("SOLUTION_BUCKET", "")
SOURCE_USER = os.environ.get("REDSHIFT_SOURCE_USER", "IAMR:DataAgentRedshiftSourceRole")


def stage(period: str) -> str:
    """Write the curated month as one Parquet file the COPY can read."""
    import duckdb

    c = duckdb.connect()
    c.execute("SET home_directory='/tmp'")  # nosec B108
    c.execute("LOAD httpfs; LOAD aws")
    c.execute(
        "CREATE OR REPLACE SECRET aws_default "
        f"(TYPE s3, PROVIDER credential_chain, REGION '{REGION}')"
    )
    target = f"s3://{BUCKET}/ops/redshift/cur-{period}.parquet"
    c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- operator env, fixed column list
        f"""COPY (
          SELECT billing_period, line_item_usage_account_id, line_item_product_code,
                 product_region_code, line_item_usage_type, line_item_operation,
                 line_item_line_item_type, line_item_resource_id, line_item_usage_amount,
                 line_item_unblended_cost
          FROM read_parquet('s3://{BUCKET}/ops/cur/billing_period={period}/*.parquet')
        ) TO '{target}' (FORMAT parquet, COMPRESSION zstd)"""  # nosec B608 - operator literals
    )
    return target


def statements(staged: str) -> list[str]:
    return [
        "CREATE SCHEMA IF NOT EXISTS ops",
        """CREATE TABLE IF NOT EXISTS ops.cur_line_items (
              billing_period VARCHAR(16), line_item_usage_account_id VARCHAR(20),
              line_item_product_code VARCHAR(64), product_region_code VARCHAR(32),
              line_item_usage_type VARCHAR(128), line_item_operation VARCHAR(64),
              line_item_line_item_type VARCHAR(32), line_item_resource_id VARCHAR(512),
              line_item_usage_amount DOUBLE PRECISION,
              line_item_unblended_cost DOUBLE PRECISION)""",
        "TRUNCATE ops.cur_line_items",
        f"""COPY ops.cur_line_items FROM '{staged}' IAM_ROLE '{ROLE}' FORMAT AS PARQUET""",
        f'CREATE USER "{SOURCE_USER}" PASSWORD DISABLE',  # may already exist
        f'GRANT USAGE ON SCHEMA ops TO "{SOURCE_USER}"',
        f'GRANT SELECT ON ALL TABLES IN SCHEMA ops TO "{SOURCE_USER}"',
        "SELECT count(*) AS rows, round(sum(line_item_unblended_cost), 2) AS total "
        "FROM ops.cur_line_items",
    ]


def run(sql):
    r = rd.execute_statement(WorkgroupName=WG, Database=DB, Sql=sql)
    sid = r["Id"]
    while True:
        d = rd.describe_statement(Id=sid)
        if d["Status"] in ("FINISHED", "FAILED", "ABORTED"):
            break
        time.sleep(2)  # nosemgrep: arbitrary-sleep -- Data API poll backoff
    if d["Status"] != "FINISHED":
        if "already exists" in str(d.get("Error", "")):
            print("OK (exists):", sql[:60])
            return []
        print("FAILED:", sql.split("\n")[0][:80], "|", d.get("Error", "")[:300])
        return None
    if d.get("HasResultSet"):
        res = rd.get_statement_result(Id=sid)
        cols = [c["name"] for c in res["ColumnMetadata"]]
        rows = [[list(v.values())[0] for v in rec] for rec in res["Records"]]
        print(
            "OK:",
            sql.split("\n")[0][:60],
            "→",
            dict(zip(cols, rows[0], strict=False)) if rows else "no rows",
        )
        return rows
    print("OK:", sql.split("\n")[0][:70], f"({d.get('ResultRows', 0)} rows)")
    return []


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--period", default="2026-08", help="closed billing period, YYYY-MM")
    args = ap.parse_args()
    if not ROLE or not BUCKET:
        sys.exit("set REDSHIFT_ROLE_ARN and SOLUTION_BUCKET")
    staged = stage(args.period)
    print("staged", staged)
    for sql in statements(staged):
        if run(sql) is None:
            sys.exit(1)


if __name__ == "__main__":
    main()
