"""The on-chain labels / sanctions REST source behind AgentCore Gateway.

A stand-in for the commercial attribution feed an exchange subscribes to: it is
NOT on S3, it is an HTTP API with its own credentials, and the agent must never
hold those credentials. The Gateway holds them (or, here, an IAM role), the
agent calls the Gateway tool with the CALLER's token, and the JSON result lands
in the session working set as `_r_<n>` — never ATTACHed into the engine.

Two tools:
  screen_addresses(addresses[])  → one row per address: is it sanctioned, which
                                   programme, which exchange labels it, risk tier
  label_lookup(entity)           → the addresses a named entity is known by

The data here is the same synthetic/public material the demo already uses (the
OFAC SDN crypto list and the synthetic exchange labels), served through a
different shape on purpose: the point of this source is the PATH, not new facts.
"""

from __future__ import annotations

import json
import os
import re

import boto3

BUCKET = os.environ.get("LABELS_BUCKET", "")
PREFIX = os.environ.get("LABELS_PREFIX_KEY", "labels")
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ENTITY_RE = re.compile(r"^[A-Za-z0-9 _.#-]{1,64}$")
MAX_ADDRESSES = 200

_cache: dict[str, list[dict]] = {}


def _rows(table: str) -> list[dict]:
    """Read one small Parquet table through S3 Select-free plain GET + pyarrow?
    No: keep the Lambda dependency-free by reading the JSON sidecars the loader
    writes next to the Parquet (labels/<table>.json)."""
    if table not in _cache:
        s3 = boto3.client("s3")
        body = s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}/{table}.json")["Body"].read()
        _cache[table] = json.loads(body)
    return _cache[table]


def screen_addresses(addresses: list[str]) -> dict:
    clean = [a.lower() for a in addresses if isinstance(a, str) and _ADDRESS_RE.match(a)]
    if not clean:
        return {"error": "no valid 0x-prefixed 40-hex addresses supplied", "rows": []}
    clean = clean[:MAX_ADDRESSES]
    ofac = {r["address"].lower(): r for r in _rows("ofac_addresses")}
    labels: dict[str, dict] = {}
    for r in _rows("cex_deposit_labels"):
        labels.setdefault(r["address"].lower(), r)
    rows = []
    for a in clean:
        o, lab = ofac.get(a), labels.get(a)
        rows.append(
            {
                "address": a,
                "sanctioned": bool(o),
                "program": (o or {}).get("program"),
                "exchange": (lab or {}).get("exchange"),
                "label": (lab or {}).get("label"),
                "risk_tier": (lab or {}).get("risk_tier") or ("high" if o else "unknown"),
                "source": "labels-api/v1",
            }
        )
    return {"rows": rows, "count": len(rows), "source": "labels-api/v1"}


def label_lookup(entity: str) -> dict:
    if not isinstance(entity, str) or not _ENTITY_RE.match(entity):
        return {"error": "entity must be 1-64 chars of [A-Za-z0-9 _.#-]", "rows": []}
    needle = entity.lower()
    rows = [
        {
            "address": r["address"],
            "exchange": r.get("exchange"),
            "label": r.get("label"),
            "risk_tier": r.get("risk_tier"),
            "source": "labels-api/v1",
        }
        for r in _rows("cex_deposit_labels")
        if needle in (r.get("exchange", "") + " " + r.get("label", "")).lower()
    ]
    return {"rows": rows, "count": len(rows), "source": "labels-api/v1"}


def lambda_handler(event, context):
    """AgentCore Gateway Lambda target: the tool name arrives in the context's
    client context / event, the arguments as the event body."""
    tool = ""
    cc = getattr(context, "client_context", None)
    if cc is not None:
        tool = (getattr(cc, "custom", None) or {}).get("bedrockAgentCoreToolName", "")
    tool = (event.get("toolName") or tool or "").split("___")[-1]
    args = event if isinstance(event, dict) else {}
    if tool == "screen_addresses":
        return screen_addresses(args.get("addresses") or [])
    if tool == "label_lookup":
        return label_lookup(args.get("entity") or "")
    return {"error": f"unknown tool {tool!r}", "tools": ["screen_addresses", "label_lookup"]}
