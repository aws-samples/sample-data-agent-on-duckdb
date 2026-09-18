"""The warehouse source behind AgentCore Gateway.

Amazon Redshift Serverless holds the same cloud-ops CUR month as the lake. The
agent reaches it as a READ-ONLY TOOL through the Gateway — never as a DuckDB
attachment — and the rows land in the session working set as `_r_<n>`.

Deliberate design choice: this tool does NOT accept free SQL. The agent's
row/column governance is compiled into the SQL it sends to ITS OWN engine; a
warehouse reached over a service credential would be an ungoverned side channel
if it accepted arbitrary statements. So the surface is a fixed, parameterized
tool set (the Data API binds every parameter), each query aggregate-only:

  cost_by_service(billing_period, limit)   → service totals for one month
  cost_total(billing_period)               → one row: rows + total
  cost_by_usage_type(billing_period, service, limit)

Per-user filtering at the source needs IAM Identity Center trusted identity
propagation (not deployed here), so the audit line marks results from this tool
`degraded=2lo`: the warehouse saw a service identity, and the agent applies the
caller's filter on the landed table afterwards.
"""

from __future__ import annotations

import os
import re
import time

import boto3

WORKGROUP = os.environ.get("REDSHIFT_WORKGROUP", "data-agent")
DATABASE = os.environ.get("REDSHIFT_DATABASE", "dev")
SCHEMA = os.environ.get("REDSHIFT_SCHEMA", "ops")
TIMEOUT_S = float(os.environ.get("REDSHIFT_TIMEOUT_S", "45"))
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_SERVICE_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,64}$")

QUERIES = {
    "cost_by_service": (
        f"SELECT line_item_product_code AS service, "  # nosec B608 - fixed schema name from env
        f"round(sum(line_item_unblended_cost), 2) AS unblended_cost_usd "
        f"FROM {SCHEMA}.cur_line_items WHERE billing_period = :period "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT :row_limit"
    ),
    "cost_total": (
        f"SELECT count(*) AS line_items, "  # nosec B608 - fixed schema name from env
        f"round(sum(line_item_unblended_cost), 2) AS unblended_cost_usd "
        f"FROM {SCHEMA}.cur_line_items WHERE billing_period = :period"
    ),
    "cost_by_usage_type": (
        f"SELECT line_item_usage_type AS usage_type, "  # nosec B608 - fixed schema name from env
        f"round(sum(line_item_unblended_cost), 2) AS unblended_cost_usd "
        f"FROM {SCHEMA}.cur_line_items WHERE billing_period = :period "
        "AND line_item_product_code = :service GROUP BY 1 ORDER BY 2 DESC LIMIT :row_limit"
    ),
}


def _run(sql: str, params: list[dict]) -> dict:
    rd = boto3.client("redshift-data")
    stmt = rd.execute_statement(
        WorkgroupName=WORKGROUP, Database=DATABASE, Sql=sql, Parameters=params
    )["Id"]
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        d = rd.describe_statement(Id=stmt)
        if d["Status"] in ("FINISHED", "FAILED", "ABORTED"):
            break
        time.sleep(0.4)  # nosemgrep: arbitrary-sleep -- Data API poll backoff
    else:
        return {"error": f"redshift statement timed out after {TIMEOUT_S}s", "rows": []}
    if d["Status"] != "FINISHED":
        return {"error": f"redshift: {str(d.get('Error'))[:300]}", "rows": []}
    res = rd.get_statement_result(Id=stmt)
    names = [c["name"] for c in res["ColumnMetadata"]]
    rows = []
    for rec in res["Records"]:
        values = []
        for cell in rec:
            values.append(None if cell.get("isNull") else next(iter(cell.values())))
        rows.append(dict(zip(names, values, strict=False)))
    return {
        "rows": rows,
        "count": len(rows),
        "source": f"redshift-serverless/{WORKGROUP}",
        "engine": "Amazon Redshift Serverless",
    }


def lambda_handler(event, context):
    tool = ""
    cc = getattr(context, "client_context", None)
    if cc is not None:
        tool = (getattr(cc, "custom", None) or {}).get("bedrockAgentCoreToolName", "")
    tool = (event.get("toolName") or tool or "").split("___")[-1]
    if tool not in QUERIES:
        return {"error": f"unknown tool {tool!r}", "tools": sorted(QUERIES)}
    period = str(event.get("billing_period", ""))
    if not _PERIOD_RE.match(period):
        return {"error": "billing_period must look like YYYY-MM", "rows": []}
    params = [{"name": "period", "value": period}]
    if "row_limit" in QUERIES[tool]:
        try:
            row_limit = max(1, min(int(event.get("limit", 20)), 500))
        except (TypeError, ValueError):
            return {"error": "limit must be an integer between 1 and 500", "rows": []}
        params.append({"name": "row_limit", "value": str(row_limit)})
    if ":service" in QUERIES[tool]:
        service = str(event.get("service", ""))
        if not _SERVICE_RE.match(service):
            return {"error": "service must be 1-64 chars of [A-Za-z0-9 _.-]", "rows": []}
        params.append({"name": "service", "value": service})
    return _run(QUERIES[tool], params)
