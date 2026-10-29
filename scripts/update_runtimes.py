#!/usr/bin/env python3
"""Point both AgentCore runtimes at a new image, preserving everything else.

The deploy path that does not need the starter-toolkit CLI: build the image with the
account's CodeBuild project, then run this to roll BOTH runtimes — the HTTP one
the UI and the eval use, and the MCP one coding agents connect to — onto the
new tag.

Each runtime is re-submitted with its OWN live configuration and only
`containerUri` changed, so the environment set, the JWT authorizer, the server
protocol, the network mode and the header allowlist cannot be silently dropped.
That matters here: `agentcore launch` REPLACES the env on every launch (hence
the "pass all of them" warning in the operating manual), and the MCP runtime
was created by a control-plane call in the first place.

  python scripts/update_runtimes.py <image-tag> [--runtime <id> ...]
"""

from __future__ import annotations

import argparse
import json
import os

import boto3

ECR = "bedrock-agentcore-duckdb_analyst"
# runtime ids of YOUR deployment (comma-separated), or pass --runtime; no defaults
DEFAULT_RUNTIMES = tuple(r for r in os.environ.get("AGENT_RUNTIME_IDS", "").split(",") if r)
# config keys carried over verbatim; absent ones are simply not sent
CARRIED = (
    "protocolConfiguration",
    "authorizerConfiguration",
    "environmentVariables",
    "requestHeaderConfiguration",
    "description",
)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("tag", help="ECR image tag to roll onto (e.g. 20261018-061202)")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--account", default=None, help="defaults to the caller's account")
    ap.add_argument(
        "--runtime", action="append", default=None, help="repeatable; default: $AGENT_RUNTIME_IDS"
    )
    args = ap.parse_args()

    session = boto3.Session(region_name=args.region)
    account = args.account or session.client("sts").get_caller_identity()["Account"]
    uri = f"{account}.dkr.ecr.{args.region}.amazonaws.com/{ECR}:{args.tag}"
    control = session.client("bedrock-agentcore-control")

    runtimes = args.runtime or DEFAULT_RUNTIMES
    if not runtimes:
        ap.error("name the runtimes: --runtime <id> (repeatable) or AGENT_RUNTIME_IDS")
    for rid in runtimes:
        cur = control.get_agent_runtime(agentRuntimeId=rid)
        kwargs = {
            "agentRuntimeId": rid,
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": uri}},
            "roleArn": cur["roleArn"],
            "networkConfiguration": cur["networkConfiguration"],
            **{k: cur[k] for k in CARRIED if cur.get(k)},
        }
        out = control.update_agent_runtime(**kwargs)
        print(
            json.dumps(
                {
                    "runtime": rid,
                    "from": cur["agentRuntimeArtifact"]["containerConfiguration"][
                        "containerUri"
                    ].rsplit(":", 1)[-1],
                    "to": args.tag,
                    "version": out.get("agentRuntimeVersion"),
                    "status": out.get("status"),
                    "env_vars_kept": len(kwargs.get("environmentVariables") or {}),
                    "protocol": (kwargs.get("protocolConfiguration") or {}).get("serverProtocol"),
                }
            )
        )


if __name__ == "__main__":
    main()
