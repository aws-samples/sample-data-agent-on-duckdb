"""Enterprise cloud-ops investigation on the account's own AWS exports.

Three AWS-native exports that already land on S3 — CUR 2.0, CloudTrail, a
CloudWatch Logs export — curated to partitioned Parquet by DuckDB itself
(scripts/load_ops_data.py) under OPS_PREFIX, plus the native CUR export read
in place (zero ETL) and a table macro over the native CloudTrail gz JSON.

Same agent, same governance; the identity is an IAM principal instead of a
wallet, RLS scopes by account, CLS hides source IPs and user agents.

Personas (default policies, overridable via GOVERNANCE_POLICIES):
  ops:finops        — cost analyst: own account only, no line_item_resource_id
  ops:secops        — security analyst: own account's CloudTrail, all columns
  ops:secops-junior — same rows, but source_ip_address / user_agent denied
RLS pins every ops table to OPS_ACCOUNT_ID; with it unset the predicate can
never match — fail closed, never "all accounts".
"""

from __future__ import annotations

import os
import re

from data_agent.semantic import assets

from .. import card_assets
from .base import Scenario

OPS_PREFIX = os.environ.get("OPS_PREFIX", "")  # s3://<solution-bucket>/ops
OPS_CUR_SRC = os.environ.get("OPS_CUR_SRC", "")  # s3://<cur-bucket>/<prefix>/<export>/data
OPS_CLOUDTRAIL_SRC = os.environ.get(
    "OPS_CLOUDTRAIL_SRC", ""
)  # s3://<trail-bucket>/AWSLogs/<acct>/CloudTrail
OPS_ACCOUNT_ID = os.environ.get("OPS_ACCOUNT_ID", "")
# a partition that exists, for the data map's bounded single-partition DESCRIBE
# (the on-chain default 2025-02-21 matches nothing here — a live reconcile finding)
OPS_PROBE_MONTH = os.environ.get("OPS_PROBE_MONTH", "2026-08")
OPS_PROBE_DAY = os.environ.get("OPS_PROBE_DAY", "2026-09-02")
_PQ = "hive_partitioning=1, union_by_name=true"


def account_id(raw: str = OPS_ACCOUNT_ID) -> str:
    """Digits only, or a value that can never match so an unset var fails closed."""
    return re.sub(r"\D", "", raw) or "unset-account"


def build_views(prefix: str = OPS_PREFIX, cur_src: str = OPS_CUR_SRC) -> dict[str, str]:
    """View defs for the ops scenario; empty when it is not enabled."""
    views: dict[str, str] = {}
    if prefix:
        base = prefix.rstrip("/")
        views["cur_line_items"] = f"read_parquet('{base}/cur/*/*.parquet', {_PQ})"
        views["cloudtrail_events"] = f"read_parquet('{base}/cloudtrail/*/*/*.parquet', {_PQ})"
        views["cw_logs"] = f"read_parquet('{base}/cwlogs/*/*.parquet', {_PQ})"
    if cur_src:
        views["cur_raw"] = (
            f"read_csv('{cur_src.rstrip('/')}/BILLING_PERIOD=*/*.csv.gz', header=true, "
            "all_varchar=true, union_by_name=true, hive_partitioning=1)"
        )
    return views


def build_sources(prefix: str = OPS_PREFIX, cur_src: str = OPS_CUR_SRC) -> dict[str, dict]:
    """Cost-policy specs for the ops views (partition column + path expansion)."""
    out: dict[str, dict] = {}
    if prefix:
        base = prefix.rstrip("/")
        out["cur_line_items"] = {
            "column": "billing_period",
            "granularity": "month",
            "base": f"{base}/cur",
            "layout": "{base}/billing_period={part}/*.parquet",
            "reader": "read_parquet",
            "args": _PQ,
            "probe": OPS_PROBE_MONTH,
        }
        out["cloudtrail_events"] = {
            "column": "date",
            "granularity": "day",
            "base": f"{base}/cloudtrail",
            "layout": "{base}/date={part}/*/*.parquet",
            "reader": "read_parquet",
            "args": _PQ,
            "probe": OPS_PROBE_DAY,
        }
        out["cw_logs"] = {
            "column": "date",
            "granularity": "day",
            "base": f"{base}/cwlogs",
            "layout": "{base}/date={part}/*.parquet",
            "reader": "read_parquet",
            "args": _PQ,
            "probe": OPS_PROBE_DAY,
        }
    if cur_src:
        out["cur_raw"] = {
            # hive column BILLING_PERIOD (identifiers are case-insensitive)
            "column": "billing_period",
            "granularity": "month",
            "base": cur_src.rstrip("/"),
            "layout": "{base}/BILLING_PERIOD={part}/*.csv.gz",
            "reader": "read_csv",
            "args": "header=true, all_varchar=true, union_by_name=true, hive_partitioning=1",
            "probe": OPS_PROBE_MONTH,
        }
    return out


def cloudtrail_macro(cloudtrail_src: str) -> tuple[str, ...]:
    """`cloudtrail_raw(day, region)` — the native gz JSON of one region-day, in place."""
    if not cloudtrail_src:
        return ()
    base = cloudtrail_src.rstrip("/")
    return (
        "CREATE OR REPLACE MACRO cloudtrail_raw(day, region := 'us-west-2') AS TABLE "  # nosec B608 - operator env
        "SELECT unnest(Records) AS record "
        f"FROM read_json('{base}/' || region || '/' || "
        "strftime(CAST(day AS DATE), '%Y/%m/%d') || '/*.json.gz', "
        "columns={'Records': 'JSON[]'}, maximum_object_size=67108864)",
    )


DOC = """
## Cloud-ops data (this AWS account's own exports, landed on S3)

| Table (view) | Partition (REQUIRED in WHERE) | What it is | Key columns |
|---|---|---|---|
| cur_line_items | `billing_period` ('YYYY-MM') | AWS Cost & Usage Report 2.0, hourly line items, curated Parquet | line_item_usage_account_id, line_item_product_code, product_servicecode, product_region_code, line_item_usage_type, line_item_operation, line_item_resource_id, line_item_usage_amount, line_item_unblended_cost (USD), line_item_line_item_type, line_item_usage_start_date |
| cloudtrail_events | `date` ('YYYY-MM-DD') | CloudTrail management events, one row each, curated Parquet (also `region` column) | event_time, event_source, event_name, aws_region, user_type, user_arn, principal_id, user_name, access_key_id, session_issuer_arn, session_issuer_name, invoked_by, source_ip_address, user_agent, error_code, error_message, read_only, request_parameters (JSON text), resources (JSON text) |
| cw_logs | `date` | CloudWatch Logs export of the agent runtime's own log group | ts, message, log_stream |
| cur_raw | `billing_period` | the native CUR 2.0 gzip CSV as delivered (all columns VARCHAR) — zero-ETL path | as cur_line_items: line_item_usage_account_id, line_item_product_code, line_item_unblended_cost, line_item_usage_start_date |

Cloud-ops rules: cost columns are USD; `line_item_line_item_type = 'Usage'` isolates
usage charges (others: Tax, Credit, Fee, ...); rows are hourly — aggregate with
sum(). A CloudTrail investigation has the SAME shape as a fund-flow case: anchor
on an IAM principal (user_arn / session_issuer_arn / access_key_id) or a resource,
expand hop by hop (AssumeRole → session_issuer → the role's calls → resources →
errors → source IPs), keep the working set in a TEMP TABLE, join cost by
line_item_resource_id.
"""  # noqa: E501 — markdown table rows in the system-prompt doc


# ---------------------------------------------------------------- cards
# Table cards are a customer-owned Git asset: semantic/cards/cloudops.yaml
# (CUR 2.0 has 116 columns — the CUR cards name the documented subset with
# complete: false; cw_logs is admin-only by omission).
CARDS = card_assets.load_cards("cloudops")  # semantic/cards/cloudops.yaml, fail-closed


# -------------------------------------------------------------- metrics
# Semantic layer, externalized as Git assets: the FinOps / SecOps KPIs
# a reviewer signed off on live in `semantic/cloudops.yaml` — a Git asset in
# an open format, changed through review and the asset regression gate — and
# are loaded here at import, fail-closed. Column names are reconciled against
# the cards above (tests/unit/test_semantic.py).
METRICS = assets.load_scenario_metrics("cloudops")


def build_policies(account: str) -> dict:
    return {
        "ops:finops": {
            "allow_raw_paths": False,
            "tables": {
                # resource-level detail is restricted for FinOps analysts
                "cur_line_items": {
                    "row_filter": f"line_item_usage_account_id = '{account}'",
                    "deny_columns": ["line_item_resource_id"],
                },
                "cur_raw": {
                    "row_filter": f"line_item_usage_account_id = '{account}'",
                    "deny_columns": ["line_item_resource_id"],
                },
            },
            # outbound: the warehouse close is FinOps business; the
            # labels/sanctions API is not. Harmless while the redshift target
            # is torn down (#18) — an absent target simply never lists.
            "sources": ["redshift"],
        },
        "ops:secops": {
            "allow_raw_paths": False,
            "tables": {
                "cloudtrail_events": {"row_filter": f"recipient_account_id = '{account}'"},
            },
        },
        "ops:secops-junior": {
            "allow_raw_paths": False,
            "tables": {
                # junior analysts see the events but not who connected from where
                "cloudtrail_events": {
                    "row_filter": f"recipient_account_id = '{account}'",
                    "deny_columns": ["source_ip_address", "user_agent"],
                },
            },
        },
    }


def build(
    prefix: str = OPS_PREFIX,
    cur_src: str = OPS_CUR_SRC,
    cloudtrail_src: str = OPS_CLOUDTRAIL_SRC,
    account: str = OPS_ACCOUNT_ID,
) -> Scenario:
    """The scenario as configured by env; lenient because exports land over time."""
    views = build_views(prefix, cur_src)
    return Scenario(
        name="cloudops",
        views=views,
        sources=build_sources(prefix, cur_src),
        doc=DOC if views else "",
        policies=build_policies(account_id(account)),
        cards=CARDS,
        metrics=METRICS,
        macros=cloudtrail_macro(cloudtrail_src),
        lenient=True,
    )


SCENARIO = build()
