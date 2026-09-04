"""Data agent on in-process DuckDB — composition root and AgentCore entrypoint.

The analytics engine lives INSIDE the agent process: no cluster, no
warehouse, no ETL — the agent starts, attaches DuckDB, and queries data
directly on S3. The layers live in the `data_agent` package (state,
governance, context, compute, scheduling; see its docstring); this file only
wires them: the Strands agent with its tools and conversation manager, the
AgentCore app, and the two ways in.

Runs in two modes:
  - AgentCore Runtime entrypoint (default): `app.run()` serves /invocations
  - Local CLI: `python agent.py --as <tenant>:<role> "your question"`
"""

import os
import sys

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent

from data_agent.context import compression, prompt
from data_agent.scheduling import cli, runtime
from data_agent.tools import run_sql, search_schema

MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6")

# History compression: measured-pressure stubbing of materialized results;
# the manager is module-level so the result envelope can report its counters.
conversation_manager = compression.ResultStubConversationManager()
agent = Agent(
    model=MODEL_ID,
    tools=[run_sql, search_schema],
    system_prompt=prompt.SYSTEM_PROMPT,
    conversation_manager=conversation_manager,
)

app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload, context=None):
    """AgentCore Runtime entrypoint (streaming) — see `scheduling.runtime.stream`."""
    async for env in runtime.stream(agent, conversation_manager, payload, context):
        yield env


if __name__ == "__main__":
    if len(sys.argv) > 1:  # local one-shot: agent.py --as ops:admin "question"
        cli.main(sys.argv[1:], agent)
    else:
        app.run()
