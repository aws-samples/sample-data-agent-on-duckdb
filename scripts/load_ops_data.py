#!/usr/bin/env python3
"""Load the enterprise cloud-ops dataset — with DuckDB itself.

Three AWS-native exports that already land on S3 become governed, partitioned
Parquet under the solution bucket, using the same in-process engine the agent
queries with (no Glue job, no Spark, no Athena CTAS):

  cur/        AWS Data Exports "CUR 2.0" (gzip CSV per BILLING_PERIOD)
              → billing_period=YYYY-MM/part-0.parquet, key money/usage columns
              typed, everything else kept as delivered (116 columns)
  cloudtrail/ the account's CloudTrail trail (gzip JSON, Records[] per file)
              → date=YYYY-MM-DD/region=<r>/part-0.parquet, one row per event,
              identity/session/error/network fields flattened, request/response
              kept as JSON text
  cwlogs/     a CloudWatch Logs export task (gzip "ts message" lines)
              → date=YYYY-MM-DD/part-0.parquet

Run on AWS-side compute (reading from the laptop is throttled). Idempotent:
each partition is rewritten in full; a partition with no source files is
skipped. Prints a JSON summary with per-partition row counts — the numbers the
eval suite and docs cite.

Usage:
  python scripts/load_ops_data.py \
      --cur-src s3://<cur-bucket>/<prefix>/<export>/data \
      --cloudtrail-src s3://<trail-bucket>/AWSLogs/<account>/CloudTrail \
      --cwlogs-src s3://<solution-bucket>/ops/cwlogs-export \
      --out s3://<solution-bucket>/ops \
      --periods 2026-07,2026-08,2026-09 \
      --regions us-west-2,us-east-1,us-east-2 --days 2026-08-21:2026-09-03
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

# SSM/cron shells may have no HOME; DuckDB derives its extension directory from it
os.environ.setdefault("HOME", "/tmp")  # nosec B108 - extension cache location only

import duckdb  # noqa: E402

# CUR 2.0 columns worth typing (everything else stays VARCHAR as delivered).
CUR_DOUBLE = [
    "line_item_unblended_cost",
    "line_item_blended_cost",
    "line_item_net_unblended_cost",
    "line_item_usage_amount",
    "line_item_normalized_usage_amount",
    "line_item_normalization_factor",
    "pricing_public_on_demand_cost",
    "discount_total_discount",
    "discount_bundled_discount",
    "reservation_effective_cost",
    "reservation_net_effective_cost",
    "savings_plan_savings_plan_effective_cost",
    "savings_plan_net_savings_plan_effective_cost",
]
CUR_TS = [
    "bill_billing_period_start_date",
    "bill_billing_period_end_date",
    "line_item_usage_start_date",
    "line_item_usage_end_date",
]

CLOUDTRAIL_SELECT = """
  r->>'eventID'                                           AS event_id,
  TRY_CAST(replace(r->>'eventTime', 'Z', '') AS TIMESTAMP) AS event_time,
  r->>'eventSource'                                       AS event_source,
  r->>'eventName'                                         AS event_name,
  r->>'awsRegion'                                         AS aws_region,
  r->>'sourceIPAddress'                                   AS source_ip_address,
  r->>'userAgent'                                         AS user_agent,
  r->>'eventType'                                         AS event_type,
  r->>'eventCategory'                                     AS event_category,
  TRY_CAST(r->>'readOnly' AS BOOLEAN)                     AS read_only,
  TRY_CAST(r->>'managementEvent' AS BOOLEAN)              AS management_event,
  r->>'errorCode'                                         AS error_code,
  r->>'errorMessage'                                      AS error_message,
  r->>'recipientAccountId'                                AS recipient_account_id,
  r->>'requestID'                                         AS request_id,
  r->>'sharedEventID'                                     AS shared_event_id,
  r->>'vpcEndpointId'                                     AS vpc_endpoint_id,
  r->'userIdentity'->>'type'                              AS user_type,
  r->'userIdentity'->>'principalId'                       AS principal_id,
  r->'userIdentity'->>'arn'                               AS user_arn,
  r->'userIdentity'->>'accountId'                         AS user_account_id,
  r->'userIdentity'->>'accessKeyId'                       AS access_key_id,
  r->'userIdentity'->>'userName'                          AS user_name,
  r->'userIdentity'->>'invokedBy'                         AS invoked_by,
  r->'userIdentity'->'sessionContext'->'sessionIssuer'->>'arn'      AS session_issuer_arn,
  r->'userIdentity'->'sessionContext'->'sessionIssuer'->>'userName' AS session_issuer_name,
  r->'userIdentity'->'sessionContext'->'sessionIssuer'->>'type'     AS session_issuer_type,
  r->'userIdentity'->'sessionContext'->'attributes'->>'mfaAuthenticated' AS mfa_authenticated,
  CAST(r->'requestParameters' AS VARCHAR)                 AS request_parameters,
  CAST(r->'responseElements' AS VARCHAR)                  AS response_elements,
  CAST(r->'resources' AS VARCHAR)                         AS resources,
  CAST(r->'additionalEventData' AS VARCHAR)               AS additional_event_data,
  CAST(r->'tlsDetails' AS VARCHAR)                        AS tls_details
"""


def connect(default_region: str, extra_scopes: list[tuple[str, str]]) -> duckdb.DuckDBPyConnection:
    c = duckdb.connect()
    c.execute(f"SET home_directory='{os.environ['HOME']}'")  # noqa: E501  # nosemgrep: sqlalchemy-execute-raw-query -- process env, not user input
    c.execute("INSTALL httpfs; LOAD httpfs; INSTALL json; LOAD json; INSTALL aws; LOAD aws;")
    c.execute("SET memory_limit='10GB'; SET threads=8; SET preserve_insertion_order=false")
    c.execute(
        "CREATE OR REPLACE SECRET s3_default (TYPE s3, PROVIDER credential_chain, "
        f"REGION '{default_region}')"
    )
    for i, (bucket, region) in enumerate(extra_scopes):
        c.execute(
            f"CREATE OR REPLACE SECRET s3_scope_{i} (TYPE s3, PROVIDER credential_chain, "
            f"REGION '{region}', SCOPE 's3://{bucket}')"
        )
    return c


def bucket_of(uri: str) -> str:
    return uri.split("/")[2]


def files(c, glob: str) -> int:
    try:
        return c.execute(f"SELECT count(*) FROM glob('{glob}')").fetchone()[0]  # nosec B608 - loader arg  # nosemgrep: sqlalchemy-execute-raw-query -- loader arg
    except Exception:  # noqa: BLE001
        return 0


def days_between(spec: str) -> list[str]:
    a, b = spec.split(":")
    d0, d1 = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


# --------------------------------------------------------------------- CUR
def load_cur(c, src: str, out: str, periods: list[str]) -> dict:
    summary = {}
    replace = ", ".join(
        [f"TRY_CAST({col} AS DOUBLE) AS {col}" for col in CUR_DOUBLE]
        + [f"TRY_CAST(replace({col}, 'Z', '') AS TIMESTAMP) AS {col}" for col in CUR_TS]
    )
    for p in periods:
        glob = f"{src}/BILLING_PERIOD={p}/*.csv.gz"
        if files(c, glob) == 0:
            summary[p] = "skipped (no source files)"
            continue
        t0 = time.perf_counter()
        target = f"{out}/cur/billing_period={p}/part-0.parquet"
        sql = (
            f"COPY (SELECT * REPLACE ({replace}) "  # nosec B608 - loader-controlled literals
            f"FROM read_csv('{glob}', header=true, all_varchar=true, union_by_name=true)) "
            f"TO '{target}' (FORMAT parquet, COMPRESSION zstd)"
        )
        c.execute(sql)
        n = c.execute(f"SELECT count(*) FROM read_parquet('{target}')").fetchone()[0]  # nosec B608 - loader-built path  # nosemgrep: sqlalchemy-execute-raw-query -- loader-built path
        summary[p] = {"rows": n, "seconds": round(time.perf_counter() - t0, 1)}
        print(f"cur {p}: {n:,} rows in {summary[p]['seconds']}s", file=sys.stderr, flush=True)
    return summary


# -------------------------------------------------------------- CloudTrail
def load_cloudtrail(c, src: str, out: str, regions: list[str], days: list[str]) -> dict:
    summary = {}
    for day in days:
        y, m, d = day.split("-")
        for region in regions:
            glob = f"{src}/{region}/{y}/{m}/{d}/*.json.gz"
            n_files = files(c, glob)
            if n_files == 0:
                continue
            t0 = time.perf_counter()
            target = f"{out}/cloudtrail/date={day}/region={region}/part-0.parquet"
            sql = (
                f"COPY (SELECT {CLOUDTRAIL_SELECT} "  # nosec B608 - loader-controlled literals
                "FROM (SELECT unnest(Records) AS r "
                f"FROM read_json('{glob}', columns={{'Records': 'JSON[]'}}, "
                "maximum_object_size=67108864))) "
                f"TO '{target}' (FORMAT parquet, COMPRESSION zstd)"
            )
            c.execute(sql)
            n = c.execute(f"SELECT count(*) FROM read_parquet('{target}')").fetchone()[0]  # nosec B608 - loader-built path  # nosemgrep: sqlalchemy-execute-raw-query -- loader-built path
            summary[f"{day}/{region}"] = {
                "files": n_files,
                "rows": n,
                "seconds": round(time.perf_counter() - t0, 1),
            }
            print(
                f"cloudtrail {day} {region}: {n_files} files → {n:,} events in "
                f"{summary[f'{day}/{region}']['seconds']}s",
                file=sys.stderr,
                flush=True,
            )
    return summary


# ---------------------------------------------------------- CloudWatch Logs
def load_cwlogs(c, src: str, out: str) -> dict:
    """`aws logs create-export-task` output: <prefix>/<task-id>/<stream>/000000.gz,
    one 'ISO-timestamp message' line per event."""
    glob = f"{src}/**/*.gz"
    if files(c, glob) == 0:
        return {"skipped": "no export files"}
    t0 = time.perf_counter()
    ts_expr = "TRY_CAST(replace(split_part(line, ' ', 1), 'Z', '') AS TIMESTAMP)"
    sql = (
        "CREATE OR REPLACE TEMP TABLE cw AS SELECT "  # nosec B608 - loader-controlled literals
        f"{ts_expr} AS ts, "
        "substr(line, length(split_part(line, ' ', 1)) + 2) AS message, "
        "regexp_extract(filename, '/([^/]+)/[^/]+$', 1) AS log_stream, "
        f"strftime({ts_expr}, '%Y-%m-%d') AS day "
        f"FROM read_csv('{glob}', columns={{'line': 'VARCHAR'}}, header=false, delim=chr(1), "
        "quote='', escape='', filename=true, ignore_errors=true) WHERE ts IS NOT NULL"
    )
    c.execute(sql)
    days = [r[0] for r in c.execute("SELECT DISTINCT day FROM cw ORDER BY 1").fetchall()]
    summary = {}
    for day in days:
        target = f"{out}/cwlogs/date={day}/part-0.parquet"
        sql = (
            f"COPY (SELECT ts, message, log_stream FROM cw WHERE day = '{day}') "  # nosec B608 - loader literals
            f"TO '{target}' (FORMAT parquet, COMPRESSION zstd)"
        )
        c.execute(sql)
        summary[day] = c.execute(f"SELECT count(*) FROM read_parquet('{target}')").fetchone()[0]  # nosec B608 - loader-built path  # nosemgrep: sqlalchemy-execute-raw-query -- loader-built path
    summary["seconds"] = round(time.perf_counter() - t0, 1)
    print(f"cwlogs: {summary}", file=sys.stderr, flush=True)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--cur-src", default="")
    ap.add_argument("--cloudtrail-src", default="")
    ap.add_argument("--cwlogs-src", default="")
    ap.add_argument("--out", required=True, help="s3://<solution-bucket>/ops")
    ap.add_argument("--periods", default="", help="comma-separated YYYY-MM billing periods")
    ap.add_argument("--regions", default="us-west-2")
    ap.add_argument("--days", default="", help="YYYY-MM-DD:YYYY-MM-DD inclusive")
    ap.add_argument("--region", default="us-west-2", help="region of the solution bucket")
    ap.add_argument(
        "--source-region",
        default="us-east-1",
        help="region of the CUR / CloudTrail buckets (exports and trails default to us-east-1)",
    )
    args = ap.parse_args()

    scopes = [
        (bucket_of(u), args.source_region)
        for u in (args.cur_src, args.cloudtrail_src, args.cwlogs_src)
        if u
    ]
    scopes = [
        s for s in scopes if s[0] != bucket_of(args.out)
    ]  # the solution bucket uses the default
    c = connect(args.region, scopes)
    out = args.out.rstrip("/")
    summary: dict = {"out": out}
    if args.cur_src and args.periods:
        summary["cur"] = load_cur(c, args.cur_src.rstrip("/"), out, args.periods.split(","))
    if args.cloudtrail_src and args.days:
        summary["cloudtrail"] = load_cloudtrail(
            c,
            args.cloudtrail_src.rstrip("/"),
            out,
            args.regions.split(","),
            days_between(args.days),
        )
    if args.cwlogs_src:
        summary["cwlogs"] = load_cwlogs(c, args.cwlogs_src.rstrip("/"), out)
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
