#!/usr/bin/env python3
"""A/B: in-process DuckDB vs Athena on the SAME raw Parquet files.

Simulates an agent's trial-and-error sequence (5 queries, one deliberately
wrong column name) and measures what each path costs in wall-clock and scan.

Both sides read s3://aws-public-blockchain/v1.0/btc/transactions/ for one
day partition — same bytes, same files. DuckDB runs in-process (this very
python); Athena runs via StartQueryExecution + polling, which is exactly how
an MCP tool would drive it.

Usage:
  AB_ATHENA_OUTPUT=s3://<bucket>/athena-results/ python3 scripts/ab_compare.py
Writes docs/ab-results.md.
"""

import os
import sys
import time

import boto3
import duckdb

REGION = os.environ.get("AWS_REGION", "us-west-2")
OUTPUT = os.environ.get("AB_ATHENA_OUTPUT", "")
DAY = "2026-08-25"
GLUE_DB = "data_agent_ab"
S3_LOC = "s3://aws-public-blockchain/v1.0/btc/transactions/"

DDL_TABLE = f"""
CREATE EXTERNAL TABLE IF NOT EXISTS {GLUE_DB}.btc_transactions (
  txid string, fee double, output_value double,
  input_count bigint, output_count bigint, is_coinbase boolean, size bigint
)
PARTITIONED BY (`date` string)
STORED AS PARQUET
LOCATION '{S3_LOC}'
TBLPROPERTIES (
  'projection.enabled'='true',
  'projection.date.type'='date',
  'projection.date.range'='2009-01-03,NOW',
  'projection.date.format'='yyyy-MM-dd',
  'storage.location.template'='{S3_LOC}date=${{date}}/'
)
"""

# (name, duckdb_sql, athena_sql) — dialects differ only in the percentile fn
QUERIES = [
    (
        "schema probe",
        f"SELECT txid, fee, output_value FROM btc_transactions WHERE date='{DAY}' LIMIT 5",
        f"SELECT txid, fee, output_value FROM {GLUE_DB}.btc_transactions "
        f"WHERE date='{DAY}' LIMIT 5",
    ),
    (
        "WRONG COLUMN (agent mistake)",
        f"SELECT sum(total_fee) FROM btc_transactions WHERE date='{DAY}'",
        f"SELECT sum(total_fee) FROM {GLUE_DB}.btc_transactions WHERE date='{DAY}'",
    ),
    (
        "corrected aggregate",
        f"SELECT count(*) AS txs, round(sum(fee),2) AS fee_btc "
        f"FROM btc_transactions WHERE date='{DAY}'",
        f"SELECT count(*) AS txs, round(sum(fee),2) AS fee_btc "
        f"FROM {GLUE_DB}.btc_transactions WHERE date='{DAY}'",
    ),
    (
        "fee percentiles",
        f"SELECT approx_quantile(fee,0.5) AS p50, approx_quantile(fee,0.99) AS p99 "
        f"FROM btc_transactions WHERE date='{DAY}' AND NOT is_coinbase",
        f"SELECT approx_percentile(fee,0.5) AS p50, approx_percentile(fee,0.99) AS p99 "
        f"FROM {GLUE_DB}.btc_transactions WHERE date='{DAY}' AND NOT is_coinbase",
    ),
    (
        "top-10 by fee",
        f"SELECT txid, fee FROM btc_transactions WHERE date='{DAY}' AND NOT is_coinbase "
        f"ORDER BY fee DESC LIMIT 10",
        f"SELECT txid, fee FROM {GLUE_DB}.btc_transactions WHERE date='{DAY}' AND NOT is_coinbase "
        f"ORDER BY fee DESC LIMIT 10",
    ),
]


def duckdb_leg():
    c = duckdb.connect()
    c.execute("SET home_directory='/tmp'")
    c.execute("INSTALL httpfs; LOAD httpfs;")
    c.execute("CREATE SECRET b (TYPE s3, PROVIDER credential_chain, REGION 'us-east-2')")
    # single-day explicit path: matches the agent's own cost discipline, avoids
    # schema drift from 2009-era files (dataset schema evolved; full-history
    # globs take the FIRST file's schema which lacks `fee`), and mirrors what
    # Athena's partition projection reads for the same predicate.
    c.execute(
        f"CREATE VIEW btc_transactions AS SELECT *, '{DAY}' AS date "
        f"FROM read_parquet('{S3_LOC}date={DAY}/*.parquet')"
    )
    results = []
    for name, sql, _ in QUERIES:
        t0 = time.perf_counter()
        try:
            c.execute(sql).fetchall()
            ok = True
        except Exception:
            ok = False
        results.append({"q": name, "s": time.perf_counter() - t0, "ok": ok, "bytes": None})
    return results


def athena_leg():
    ath = boto3.client("athena", region_name=REGION)

    def run(sql):
        t0 = time.perf_counter()
        qid = ath.start_query_execution(
            QueryString=sql, ResultConfiguration={"OutputLocation": OUTPUT}
        )["QueryExecutionId"]
        while True:
            st = ath.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = st["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            # Intentional Athena poll interval; loop exits on terminal state.
            time.sleep(0.4)  # nosemgrep: arbitrary-sleep -- poll backoff
        stats = st.get("Statistics", {})
        return {
            "s": time.perf_counter() - t0,
            "ok": state == "SUCCEEDED",
            "bytes": stats.get("DataScannedInBytes", 0),
            "engine_ms": stats.get("EngineExecutionTimeInMillis", 0),
            "queue_ms": stats.get("QueryQueueTimeInMillis", 0),
        }

    run(f"CREATE DATABASE IF NOT EXISTS {GLUE_DB}")
    run(DDL_TABLE)
    out = []
    for name, _, sql in QUERIES:
        r = run(sql)
        r["q"] = name
        out.append(r)
    return out


def main():
    if not OUTPUT:
        sys.exit("set AB_ATHENA_OUTPUT=s3://bucket/prefix/")
    duck = duckdb_leg()
    ath = athena_leg()
    d_total = sum(r["s"] for r in duck)
    a_total = sum(r["s"] for r in ath)
    a_bytes = sum(r["bytes"] or 0 for r in ath)

    lines = [
        "# A/B: in-process DuckDB vs Athena (same Parquet files)",
        "",
        f"Scenario: agent trial-and-error sequence, 5 queries incl. 1 mistake · day={DAY}",
        f"Region: {REGION} (engine) / us-east-2 (data) · generated by scripts/ab_compare.py",
        "",
        "| # | Query | DuckDB s | Athena s | Athena queue ms | Athena scanned MB | ok(D/A) |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, (d, a) in enumerate(zip(duck, ath, strict=True), 1):
        mb = (a["bytes"] or 0) / 1e6
        lines.append(
            f"| {i} | {d['q']} | {d['s']:.2f} | {a['s']:.2f} | {a.get('queue_ms', 0)} "
            f"| {mb:.1f} | {'✓' if d['ok'] else '✗'}/{'✓' if a['ok'] else '✗'} |"
        )
    lines += [
        "",
        f"**Totals**: DuckDB {d_total:.1f}s · Athena {a_total:.1f}s "
        f"({a_total / max(d_total, 0.01):.1f}x) · Athena scanned {a_bytes / 1e6:.0f} MB",
        "",
        "Notes: identical files both sides; DuckDB timings include S3 range reads",
        "from a warm process (as an agent session would). Athena timings are the",
        "full StartQueryExecution→poll lifecycle an MCP tool pays per query —",
        "including for the failed query (#2). Athena bills per bytes scanned",
        "(see current regional price list); DuckDB's marginal cost is S3 GETs.",
        "Percentile fn differs by dialect (approx_quantile vs approx_percentile).",
    ]
    out_path = os.path.join(os.path.dirname(__file__), "..", "docs", "ab-results.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
