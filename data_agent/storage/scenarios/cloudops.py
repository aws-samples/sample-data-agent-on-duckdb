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

from data_agent.semantic.model import Metric

from .base import Scenario, TableCard

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
# Table cards. CUR 2.0 has 116 columns: the cards for cur_line_items /
# cur_raw name the documented subset (complete=False). cloudtrail_events and
# cw_logs list their full curated schema (scripts/load_ops_data.py) plus the
# hive partition columns. cw_logs is admin-only by omission.
_OPS_OWNER = "cloudops scenario (this account's exports; scripts/load_ops_data.py)"
_CUR_COLUMNS = (
    "billing_period",
    "line_item_usage_account_id",
    "line_item_product_code",
    "product_servicecode",
    "product_region_code",
    "line_item_usage_type",
    "line_item_operation",
    "line_item_resource_id",
    "line_item_usage_amount",
    "line_item_unblended_cost",
    "line_item_blended_cost",
    "line_item_net_unblended_cost",
    "line_item_line_item_type",
    "line_item_usage_start_date",
    "line_item_usage_end_date",
    "bill_billing_period_start_date",
    "bill_billing_period_end_date",
    "bill_payer_account_id",
    "line_item_currency_code",
    "pricing_public_on_demand_cost",
)
CARDS = {
    "cur_line_items": TableCard(
        grain="one row = one hourly CUR 2.0 line item (usage or charge)",
        use_when="cost by service / region / usage type / resource; month-over-month; unit costs",
        avoid_when="who did what — that is cloudtrail_events",
        caveats=(
            "costs are USD; filter line_item_line_item_type = 'Usage' to "
            "isolate usage; sum() across hourly rows; `billing_period` is 'YYYY-MM'"
        ),
        owner=_OPS_OWNER,
        columns=_CUR_COLUMNS,
        complete=False,
    ),
    "cloudtrail_events": TableCard(
        grain="one row = one CloudTrail management event",
        use_when=(
            "who called which API when, from where, with what error — IAM principal investigations"
        ),
        avoid_when="cost questions — join cur_line_items on line_item_resource_id instead",
        caveats=(
            "`date` is 'YYYY-MM-DD'; assumed roles show "
            "session_issuer_arn; request_parameters/resources are JSON text"
        ),
        owner=_OPS_OWNER,
        columns=(
            "event_id",
            "event_time",
            "event_source",
            "event_name",
            "aws_region",
            "source_ip_address",
            "user_agent",
            "event_type",
            "event_category",
            "read_only",
            "management_event",
            "error_code",
            "error_message",
            "recipient_account_id",
            "request_id",
            "shared_event_id",
            "vpc_endpoint_id",
            "user_type",
            "principal_id",
            "user_arn",
            "user_account_id",
            "access_key_id",
            "user_name",
            "invoked_by",
            "session_issuer_arn",
            "session_issuer_name",
            "session_issuer_type",
            "mfa_authenticated",
            "request_parameters",
            "response_elements",
            "resources",
            "additional_event_data",
            "tls_details",
            "date",
            "region",
        ),
    ),
    "cw_logs": TableCard(
        grain="one row = one log event of the agent runtime's own log group",
        use_when="operator forensics on the runtime itself (audit lines, errors)",
        avoid_when="any tenant-facing question — this table is operator-only",
        caveats=(
            "contains the audit log of every principal's "
            "statements; deliberately unlisted by every restricted persona"
        ),
        owner=_OPS_OWNER,
        columns=("ts", "message", "log_stream", "date"),
        admin_only=True,
    ),
    "cur_raw": TableCard(
        grain="one row = one hourly CUR 2.0 line item, native gzip CSV, all VARCHAR",
        use_when="zero-ETL check of the curated table, or a column the curated table dropped",
        avoid_when="routine cost analysis — cur_line_items is typed and 20-50× faster",
        caveats=(
            "every column is VARCHAR: CAST costs before "
            "sum(); the current month is re-delivered daily"
        ),
        owner=_OPS_OWNER,
        columns=_CUR_COLUMNS,
        complete=False,
    ),
}


# -------------------------------------------------------------- metrics
# Semantic layer v0: the FinOps / SecOps KPIs a reviewer signed
# off on, compiled deterministically by `call_metric`. Column names are
# reconciled against the cards above (tests/unit/test_semantic.py).
_FINOPS_OWNER = "FinOps (cloudops scenario)"
_SECOPS_OWNER = "SecOps (cloudops scenario)"
_COST = "round(sum(line_item_unblended_cost), 2)"
METRICS = {
    "unblended_cost_by_service": Metric(
        name="unblended_cost_by_service",
        version="1.0",
        owner=_FINOPS_OWNER,
        description="Unblended cost (USD) per billing period, grouped by service and optionally "
        "region / usage type",
        table="cur_line_items",
        measure_sql=_COST,
        measure_alias="unblended_cost_usd",
        time_column="billing_period",
        grain="month",
        dimensions={
            "service": "line_item_product_code",
            "region": "product_region_code",
            "usage_type": "line_item_usage_type",
            "line_item_type": "line_item_line_item_type",
        },
        default_dimensions=("service",),
        filters=("line_item_product_code", "product_region_code", "line_item_line_item_type"),
    ),
    "unblended_cost_by_billing_period": Metric(
        name="unblended_cost_by_billing_period",
        version="1.0",
        owner=_FINOPS_OWNER,
        description="Total unblended cost (USD) per billing period",
        table="cur_line_items",
        measure_sql=_COST,
        measure_alias="unblended_cost_usd",
        time_column="billing_period",
        grain="month",
        dimensions={"billing_period": "billing_period"},
        default_dimensions=("billing_period",),
        filters=("line_item_product_code", "line_item_line_item_type"),
    ),
    "top_resources": Metric(
        name="top_resources",
        version="1.0",
        owner=_FINOPS_OWNER,
        description="Costliest resources (line_item_resource_id) per billing period — "
        "resource-level detail, denied to the finops persona by column policy",
        table="cur_line_items",
        measure_sql=_COST,
        measure_alias="unblended_cost_usd",
        time_column="billing_period",
        grain="month",
        dimensions={"resource": "line_item_resource_id", "service": "line_item_product_code"},
        default_dimensions=("resource",),
        filters=("line_item_product_code",),
        fixed_filters=("line_item_resource_id <> ''",),
    ),
    "mom_delta": Metric(
        name="mom_delta",
        version="1.0",
        owner=_FINOPS_OWNER,
        description="Month-over-month change of total unblended cost: per-period total, "
        "delta and % change against the previous period in the window",
        table="cur_line_items",
        measure_sql=_COST,
        measure_alias="unblended_cost_usd",
        time_column="billing_period",
        grain="month",
        filters=("line_item_product_code", "line_item_line_item_type"),
        kind="period_delta",
    ),
    "assume_role_events_by_principal": Metric(
        name="assume_role_events_by_principal",
        version="1.0",
        owner=_SECOPS_OWNER,
        description="AssumeRole events per day, grouped by the assuming principal "
        "(session issuer, else user ARN, else invoking service)",
        table="cloudtrail_events",
        measure_sql="count(*)",
        measure_alias="events",
        time_column="date",
        grain="day",
        dimensions={
            "principal": "coalesce(session_issuer_arn, user_arn, invoked_by)",
            "region": "aws_region",
            "error_code": "error_code",
        },
        default_dimensions=("principal",),
        filters=("aws_region", "error_code", "user_type"),
        fixed_filters=("event_name = 'AssumeRole'",),
    ),
    "runtime_log_events_by_stream": Metric(
        name="runtime_log_events_by_stream",
        version="1.0",
        owner="platform operators (cloudops scenario)",
        description="Log events of the agent runtime's own log group per day, by log stream "
        "— operator-only (cw_logs is admin-only by omission)",
        table="cw_logs",
        measure_sql="count(*)",
        measure_alias="events",
        time_column="date",
        grain="day",
        dimensions={"log_stream": "log_stream"},
        default_dimensions=("log_stream",),
    ),
}


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
