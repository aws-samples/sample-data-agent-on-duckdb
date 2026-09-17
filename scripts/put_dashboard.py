#!/usr/bin/env python3
"""Deploy the CDK-defined CloudWatch dashboard without deploying the whole
stack.

`infra/stack.py` is the source of truth for the dashboard: this script SYNTHS it
and PUTs the resulting body, resolving the two CloudFormation refs (region, the
runtime log group). It exists because this account's buckets, table bucket and
user pool were created out of band, so a full `cdk deploy` would fight them; in
a clean account `cdk deploy` covers the dashboard as well.

  python scripts/put_dashboard.py [--log-group <name>] [--dashboard <name>]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # nosec B404 - the AWS CLI is the deploy path here
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "infra"))
import aws_cdk as cdk  # noqa: E402
from stack import DataAgentStack  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument(
    "--log-group",
    default=os.environ.get("AGENT_LOG_GROUP", ""),
    help="the runtime's log group: /aws/bedrock-agentcore/runtimes/<runtime id>-DEFAULT",
)
ap.add_argument("--dashboard", default="DataAgentOnDuckDB")
ap.add_argument("--region", default="us-west-2")
args = ap.parse_args()
LOG_GROUP = args.log_group
if not LOG_GROUP:
    sys.exit("set --log-group or AGENT_LOG_GROUP (deployment-specific)")
app = cdk.App(outdir="/tmp/cdk-dashboard-synth")  # nosec B108 - synth scratch dir
DataAgentStack(app, "DataAgentOnDuckDB", description="dashboard body source")
asm = app.synth()
t = json.load(open(asm.get_stack_by_name("DataAgentOnDuckDB").template_full_path))
dash = [r for r in t["Resources"].values() if r["Type"] == "AWS::CloudWatch::Dashboard"][0]
body = dash["Properties"]["DashboardBody"]


def _resolve(x):
    """Fn::Join fragments: strings pass through; {"Ref": "AWS::Region"} is the
    region, every other ref in this body is the runtime log group."""
    if isinstance(x, str):
        return x
    if x.get("Ref") == "AWS::Region":
        return args.region
    return LOG_GROUP


if isinstance(body, dict):
    raw = body["Fn::Join"][0].join(_resolve(x) for x in body["Fn::Join"][1])
else:
    raw = body
parsed = json.loads(raw)
for w in parsed["widgets"]:  # any residual token placeholder
    props = w.get("properties", {})
    if isinstance(props.get("region"), str) and (
        props["region"].startswith("${Token") or props["region"].startswith("/aws/")
    ):
        props["region"] = args.region
print("widgets:", [w["properties"].get("title") for w in parsed["widgets"]])
AWS_CLI = shutil.which("aws") or "aws"  # absolute path when resolvable (bandit B607)
out = subprocess.run(  # noqa: S603  # nosec B603,B607 - resolved argv, no shell
    [
        AWS_CLI,
        "cloudwatch",
        "put-dashboard",
        "--region",
        args.region,
        "--dashboard-name",
        args.dashboard,
        "--dashboard-body",
        json.dumps(parsed),
    ],
    capture_output=True,
    text=True,
    check=False,
)
print("put-dashboard rc:", out.returncode, (out.stdout or out.stderr)[:300])
sys.exit(out.returncode)
