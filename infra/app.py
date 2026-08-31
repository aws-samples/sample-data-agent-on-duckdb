#!/usr/bin/env python3
"""CDK app for data-agent-on-duckdb: one stack, one command.

Provisions everything the agent needs around the AgentCore runtime:
  - S3 Tables bucket + namespace (Path B; Path C federates the same bucket)
  - DuckLake catalog/data bucket (Path D, single-file catalog form)
  - AgentCore execution role with least-privilege policies for all paths
  - CloudWatch log group for the runtime

The AgentCore runtime itself is created by `agentcore launch` (starter
toolkit) or the console — it is not yet a stable CloudFormation resource.
This stack owns everything the runtime depends on, so `cdk destroy` plus
`agentcore destroy` is a complete cleanup.
"""

import aws_cdk as cdk
from stack import DataAgentStack

app = cdk.App()
DataAgentStack(
    app,
    "DataAgentOnDuckDB",
    description="Data agent with in-process DuckDB: S3 Tables, DuckLake bucket, "
    "least-privilege execution role (uksb-anonymous-sample)",
)
app.synth()
