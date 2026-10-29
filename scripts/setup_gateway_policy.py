#!/usr/bin/env python3
"""Attach the persona source allowlist to the AgentCore Gateway as Cedar policies.

The platform half of the outbound dual allowlist: the
agent already refuses a source its persona does not list; this makes the
Gateway refuse the same call even if the agent-side check were bypassed.
The policies are RENDERED from the persona `sources` fields
(`data_agent/governance/cedar.py`), so both halves read one definition.

Idempotent — run it after any persona or gateway-target change:

    ./.venv/bin/python scripts/setup_gateway_policy.py --dry-run     # print the Cedar
    ./.venv/bin/python scripts/setup_gateway_policy.py --mode LOG_ONLY
    ./.venv/bin/python scripts/setup_gateway_policy.py --mode ENFORCE

What it does, in order:
  1. reads the gateway (by name) and every target's tool names;
  2. renders one `permit` per tool from the persona table (env
     GOVERNANCE_POLICIES respected, exactly as the agent loads it);
  3. ensures a policy engine exists and the gateway role may evaluate it;
  4. creates / updates / deletes ONLY the policies this script owns
     (name prefix `da_permit_`), waiting for each to become ACTIVE;
  5. re-submits the gateway's live configuration with only
     `policyEngineConfiguration` changed (nothing else can be dropped).

Deny-by-default in ENFORCE mode: a tool no persona lists gets no policy and
is refused at the gateway. Start with LOG_ONLY on a new deployment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import boto3  # noqa: E402

from data_agent.governance import cedar, policies  # noqa: E402

ENGINE_ROLE_POLICY = "evaluate-gateway-policy"


def _gateway(ctl, name: str) -> dict:
    gid = None
    for page in ctl.get_paginator("list_gateways").paginate():
        for g in page.get("items", []):
            if g["name"] == name:
                gid = g["gatewayId"]
    if not gid:
        sys.exit(f"no gateway named {name!r}")
    return ctl.get_gateway(gatewayIdentifier=gid)


def _tools_by_target(ctl, gateway_id: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    items = ctl.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", [])
    for item in items:
        t = ctl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=item["targetId"])
        mcp_cfg = (t.get("targetConfiguration") or {}).get("mcp") or {}
        schema = (mcp_cfg.get("lambda") or {}).get("toolSchema") or {}
        tools = [x["name"] for x in schema.get("inlinePayload") or []]
        if not tools:
            # refuse rather than guess: a target whose tools we cannot name
            # would silently get no permit (refused) or need a wildcard
            sys.exit(
                f"target {t['name']!r}: cannot read its tool names (only Lambda targets "
                "with an inline tool schema are supported by this script)"
            )
        out[t["name"]] = tools
    return out


def _ensure_engine(ctl, name: str, dry: bool) -> dict | None:
    for page in ctl.get_paginator("list_policy_engines").paginate():
        for e in page.get("policyEngines", []):
            if e["name"] == name:
                return e
    if dry:
        return None
    e = ctl.create_policy_engine(
        name=name,
        description="Cedar policies for data-agent-gateway, rendered from the persona "
        "`sources` allowlists by scripts/setup_gateway_policy.py",
    )
    return _wait(lambda: ctl.get_policy_engine(policyEngineId=e["policyEngineId"]), "engine")


def _wait(get, what: str, timeout: int = 180) -> dict:
    deadline = time.time() + timeout
    while True:
        cur = get()
        status = cur.get("status")
        if status == "ACTIVE":
            return cur
        if status and status.endswith("FAILED"):
            sys.exit(f"{what} {status}: {cur.get('statusReasons')}")
        if time.time() > deadline:
            sys.exit(f"{what} still {status} after {timeout}s")
        time.sleep(3)  # nosemgrep: arbitrary-sleep -- control-plane status poll


def _ensure_role_permission(gateway: dict, engine_arn: str, dry: bool) -> None:
    role_name = gateway["roleArn"].rsplit("/", 1)[-1]
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EvaluatePolicies",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:GetPolicyEngine",
                    "bedrock-agentcore:AuthorizeAction",
                    "bedrock-agentcore:PartiallyAuthorizeActions",
                ],
                "Resource": [engine_arn, gateway["gatewayArn"]],
            }
        ],
    }
    if dry:
        print(f"[dry-run] would put inline policy {ENGINE_ROLE_POLICY} on {role_name}")
        return
    boto3.client("iam").put_role_policy(
        RoleName=role_name, PolicyName=ENGINE_ROLE_POLICY, PolicyDocument=json.dumps(doc)
    )
    print(f"role {role_name}: {ENGINE_ROLE_POLICY} in place")


def _sync_policies(ctl, engine_id: str, rendered: list, dry: bool) -> None:
    existing = {}
    for page in ctl.get_paginator("list_policies").paginate(policyEngineId=engine_id):
        for p in page.get("policies", []):
            if p["name"].startswith(cedar.NAME_PREFIX):
                existing[p["name"]] = p
    wanted = {p.name: p for p in rendered}
    for name, pol in sorted(wanted.items()):
        cur = existing.get(name)
        have = ((cur or {}).get("definition") or {}).get("cedar", {}).get("statement")
        if cur and have == pol.statement:
            print(f"  = {name}")
            continue
        verb = "update" if cur else "create"
        print(f"  {'~' if cur else '+'} {name}")
        if dry:
            continue
        kw = dict(
            policyEngineId=engine_id,
            definition={"cedar": {"statement": pol.statement}},
            description=pol.description[:200],
            validationMode="FAIL_ON_ANY_FINDINGS",
        )
        if verb == "create":
            res = ctl.create_policy(name=name, **kw)
        else:
            res = ctl.update_policy(policyId=cur["policyId"], **kw)
        _wait(
            lambda pid=res["policyId"]: ctl.get_policy(policyEngineId=engine_id, policyId=pid),
            f"policy {name}",
        )
    for name in sorted(set(existing) - set(wanted)):
        print(f"  - {name} (no persona lists it any more)")
        if not dry:
            ctl.delete_policy(policyEngineId=engine_id, policyId=existing[name]["policyId"])


def _attach(ctl, gateway: dict, engine_arn: str, mode: str, dry: bool) -> None:
    cur = gateway.get("policyEngineConfiguration") or {}
    if cur.get("arn") == engine_arn and cur.get("mode") == mode:
        print(f"gateway: engine already attached in {mode}")
        return
    print(f"gateway: attach engine in {mode} (was {cur or 'none'})")
    if dry:
        return
    # re-submit the LIVE configuration: update_gateway replaces, so every field
    # the gateway has must travel with the change (the update_runtimes rule)
    keep = (
        "name",
        "description",
        "roleArn",
        "protocolType",
        "protocolConfiguration",
        "authorizerType",
        "authorizerConfiguration",
        "kmsKeyArn",
        "interceptorConfigurations",
        "exceptionLevel",
    )
    req = {k: gateway[k] for k in keep if gateway.get(k) is not None}
    req["policyEngineConfiguration"] = {"arn": engine_arn, "mode": mode}
    ctl.update_gateway(gatewayIdentifier=gateway["gatewayId"], **req)
    deadline = time.time() + 180
    while True:
        status = ctl.get_gateway(gatewayIdentifier=gateway["gatewayId"])["status"]
        if status == "READY":
            print("gateway: READY")
            return
        if "FAIL" in status or time.time() > deadline:
            sys.exit(f"gateway update ended in {status}")
        time.sleep(3)  # nosemgrep: arbitrary-sleep -- control-plane status poll


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway-name", default="data-agent-gateway")
    ap.add_argument("--engine-name", default="data_agent_policy")
    ap.add_argument("--mode", choices=["LOG_ONLY", "ENFORCE"], default="LOG_ONLY")
    ap.add_argument("--region", default=os.environ.get("AGENT_REGION", "us-west-2"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ctl = boto3.client("bedrock-agentcore-control", region_name=args.region)
    gateway = _gateway(ctl, args.gateway_name)
    tools = _tools_by_target(ctl, gateway["gatewayId"])
    rendered = cedar.render(policies.POLICIES, tools, gateway["gatewayArn"])
    print(f"gateway {gateway['gatewayId']}: targets {tools}")
    print(f"rendered {len(rendered)} permit policies from {len(policies.POLICIES)} personas")
    if args.dry_run:
        for p in rendered:
            print(f"\n-- {p.name}: {p.description}\n{p.statement}")
    engine = _ensure_engine(ctl, args.engine_name, args.dry_run)
    if engine is None:
        print(f"[dry-run] would create policy engine {args.engine_name}")
        return 0
    _ensure_role_permission(gateway, engine["policyEngineArn"], args.dry_run)
    _sync_policies(ctl, engine["policyEngineId"], rendered, args.dry_run)
    _attach(ctl, gateway, engine["policyEngineArn"], args.mode, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
