"""The metric registry: one frozen definition per business number.

A metric entry is the whole definition — which table, which measure, which
rows are in scope, which dimensions may slice it, who owns it, and what the
number means. The compiler reads this and nothing else, so the model cannot
reach a number by a route the registry did not describe.

Two properties are the point of keeping it here rather than in the prompt:

- **The definition is code, not prose.** `docs/design.md` states the incident
  figure the eval asserts (401,346.77 ETH inbound). That number depends on
  wei division, on excluding reverted traces, and on reading `eth_traces`
  rather than `eth_transactions`. Written as prompt guidance, each of those
  is a thing the model may forget on any given turn; written here, they are
  the only way the number can be computed.
- **Not-covered is explicit.** An unknown metric, a dimension the metric does
  not support, or a time window it cannot honour is refused at compile time
  with the available names listed. The alternative is a plausible number
  computed from a definition nobody agreed to.

`time_column` / `granularity` / `anchor` exist because of the cost layer: a
relative window is resolved to LITERAL dates before the SQL is emitted, so
`context.cost` can expand it into explicit per-partition paths. A subquery
anchor (`date <= (SELECT max(...) FROM ...)`) satisfies the required-filter
gate and then defeats the expansion — see `context/cost.py:86` — which is a
whole-prefix S3 LIST by another name.
"""

from __future__ import annotations

import os

# Anchor for relative windows ('last_30d' means "the 30 days ending here").
# Per-metric `anchor` wins when set; this env var is for datasets whose useful
# end date is deployment-specific (the cloud-ops exports). With neither, a
# relative window is refused and the caller is told to pass start/end — never
# silently resolved to today, which on a static sample dataset reads empty.
ANCHOR = os.environ.get("SEMANTIC_ANCHOR", "").strip()

# Relative windows, in partitions of the metric's own granularity. A metric
# declares one granularity; asking for a window of the other kind is refused
# rather than converted, because "the last 30 days of a monthly billing
# export" has no single right answer.
DAY_WINDOWS = {"last_7d": 7, "last_30d": 30, "last_90d": 90}
MONTH_WINDOWS = {"last_1m": 1, "last_3m": 3, "last_6m": 6}

# Every window the surface accepts, for error messages and the prompt catalog.
TIME_WINDOWS = tuple(DAY_WINDOWS) + tuple(MONTH_WINDOWS)

# The incident the on-chain scenario is built around (2025-02-21 onwards; the
# investigator policy's RLS lower bound is the same date). Used as the anchor
# for the on-chain metrics so a relative window lands on data that exists.
_ONCHAIN_ANCHOR = "2025-02-24"

# Successful value-moving internal calls. Both halves change the number:
# reverted traces still appear in the table, and delegatecall/staticcall rows
# carry the parent call's value, which double-counts a transfer.
_SETTLED_TRACE = ("status = 1", "call_type = 'call'")

METRICS: dict[str, dict] = {
    # ------------------------------------------------------------- on-chain
    "inbound_eth": {
        "label": "Inbound ETH",
        "scenario": "onchain",
        "owner": "AML investigations",
        "table": "eth_traces",
        "measure": "SUM(value) / 1e18",
        "unit": "ETH",
        "time_column": "date",
        "granularity": "day",
        "anchor": _ONCHAIN_ANCHOR,
        "constant_filters": _SETTLED_TRACE,
        "params": {"address": {"column": "to_address", "required": True, "lower": True}},
        "dimensions": {"date": "date", "counterparty": "from_address"},
        "definition": (
            "Value credited to the address by successful internal calls, in ETH "
            "(wei / 1e18). Reverted traces (status <> 1) and non-call trace types are "
            "excluded. Read from eth_traces, not eth_transactions: a contract-mediated "
            "credit never appears in the top-level transaction table."
        ),
        "version": "v1",
    },
    "outbound_eth": {
        "label": "Outbound ETH",
        "scenario": "onchain",
        "owner": "AML investigations",
        "table": "eth_traces",
        "measure": "SUM(value) / 1e18",
        "unit": "ETH",
        "time_column": "date",
        "granularity": "day",
        "anchor": _ONCHAIN_ANCHOR,
        "constant_filters": _SETTLED_TRACE,
        "params": {"address": {"column": "from_address", "required": True, "lower": True}},
        "dimensions": {"date": "date", "counterparty": "to_address"},
        "definition": (
            "Value debited from the address by successful internal calls, in ETH "
            "(wei / 1e18). Same scope rules as inbound_eth, mirrored on from_address."
        ),
        "version": "v1",
    },
    "counterparties": {
        "label": "Distinct counterparties",
        "scenario": "onchain",
        "owner": "AML investigations",
        "table": "eth_traces",
        "measure": "COUNT(DISTINCT from_address)",
        "unit": "addresses",
        "time_column": "date",
        "granularity": "day",
        "anchor": _ONCHAIN_ANCHOR,
        "constant_filters": _SETTLED_TRACE,
        "params": {"address": {"column": "to_address", "required": True, "lower": True}},
        "dimensions": {"date": "date"},
        "definition": (
            "Distinct addresses that sent value to the address by successful internal "
            "call. Counts addresses, not traces: a fan-in of 500 traces from 3 wallets "
            "is 3."
        ),
        "version": "v1",
    },
    "sanctioned_inflow": {
        "label": "Sanctioned inflow",
        "scenario": "onchain",
        "owner": "Sanctions compliance",
        "table": "eth_traces",
        "joins": (
            "JOIN ofac_addresses ON lower(ofac_addresses.address) = lower(eth_traces.from_address)",
        ),
        "measure": "SUM(eth_traces.value) / 1e18",
        "unit": "ETH",
        "time_column": "date",
        "granularity": "day",
        "anchor": _ONCHAIN_ANCHOR,
        "constant_filters": ("eth_traces.status = 1", "eth_traces.call_type = 'call'"),
        "params": {"address": {"column": "eth_traces.to_address", "required": True, "lower": True}},
        "dimensions": {"program": "ofac_addresses.program", "date": "eth_traces.date"},
        "definition": (
            "Of the value credited to the address, the part sent by an address on the "
            "public OFAC SDN list. Screening is a join on lower(address) against "
            "ofac_addresses; an address absent from that list is not evidence of "
            "anything, only absence of a match."
        ),
        "version": "v1",
    },
    # ------------------------------------------------------------ cloud-ops
    "unblended_spend": {
        "label": "Unblended spend",
        "scenario": "cloudops",
        "owner": "FinOps",
        "table": "cur_line_items",
        "measure": "SUM(line_item_unblended_cost)",
        "unit": "USD",
        "time_column": "billing_period",
        "granularity": "month",
        "anchor": None,  # deployment-specific: SEMANTIC_ANCHOR or explicit start/end
        "constant_filters": ("line_item_line_item_type = 'Usage'",),
        "params": {},
        "dimensions": {
            "service": "product_servicecode",
            "region": "product_region_code",
            "account": "line_item_usage_account_id",
            "usage_type": "line_item_usage_type",
        },
        "definition": (
            "Unblended cost of usage line items, in USD. Only "
            "line_item_line_item_type = 'Usage': Tax, Credit, Fee and RIFee rows are "
            "excluded, so this is what the usage cost, not what the invoice totalled. "
            "CUR rows are hourly, so the figure is a sum over the period."
        ),
        "version": "v1",
    },
    "failed_api_calls": {
        "label": "Failed API calls",
        "scenario": "cloudops",
        "owner": "SecOps",
        "table": "cloudtrail_events",
        "measure": "COUNT(*)",
        "unit": "calls",
        "time_column": "date",
        "granularity": "day",
        "anchor": None,
        "constant_filters": ("error_code IS NOT NULL",),
        "params": {},
        "dimensions": {
            "event_source": "event_source",
            "event_name": "event_name",
            "principal": "user_arn",
            "source_ip": "source_ip_address",
            "error_code": "error_code",
        },
        "definition": (
            "CloudTrail management events that returned an error, counted one per "
            "event. A denied call and a malformed call both count: error_code carries "
            "which, so slice by it before reading the total as 'attacks'."
        ),
        "version": "v1",
    },
    "api_error_rate": {
        "label": "API error rate",
        "scenario": "cloudops",
        "owner": "SecOps",
        "table": "cloudtrail_events",
        "type": "ratio",
        "numerator": "COUNT(*) FILTER (WHERE error_code IS NOT NULL)",
        "denominator": "COUNT(*)",
        "as_pct": True,
        "unit": "%",
        "time_column": "date",
        "granularity": "day",
        "anchor": None,
        "constant_filters": (),
        "params": {},
        "dimensions": {
            "event_source": "event_source",
            "principal": "user_arn",
            "date": "date",
        },
        "definition": (
            "Share of CloudTrail management events that returned an error, in percent. "
            "Denominator is every event in scope, so the rate moves with call volume; "
            "read it next to failed_api_calls, not instead of it."
        ),
        "version": "v1",
    },
}


def windows_for(granularity: str) -> dict[str, int]:
    """The relative windows a metric of this granularity accepts."""
    return DAY_WINDOWS if granularity == "day" else MONTH_WINDOWS
