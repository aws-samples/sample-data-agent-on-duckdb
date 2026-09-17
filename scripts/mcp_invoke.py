#!/usr/bin/env python3
"""Talk to the agent's MCP surface the way a coding agent does.

Logs in to Cognito as a persona, connects to the MCP-protocol runtime over
streamable HTTP with the Bearer token, and prints what an MCP client sees:
the tool list, the two resources, and the result of any tool call you name.
The runtime's own JWT authorizer validates the token before agent code runs;
identity on this path is the token and nothing else.

  source scripts/demo_env.sh
  python scripts/mcp_invoke.py <persona>                       # list tools + context
  python scripts/mcp_invoke.py junior --call 'run_sql {"sql": "SELECT 1"}'
  python scripts/mcp_invoke.py finops \\
      --call 'call_metric {"metric": "mom_delta",
                           "time_window": {"from": "2026-07", "to": "2026-08"}}'
  python scripts/mcp_invoke.py admin --resource data-agent://session
  python scripts/mcp_invoke.py --print-config   # the stanza to paste into an MCP client

Several --call flags run in order on ONE session, so a handle minted by the
first call is readable by the second.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_eval  # noqa: E402
from jwt_invoke import login  # noqa: E402


def mcp_url() -> str:
    arn = os.environ.get("AGENT_MCP_RUNTIME_ARN", "")
    if not arn:
        sys.exit("AGENT_MCP_RUNTIME_ARN is unset — run `source scripts/demo_env.sh` first")
    region = os.environ.get("AGENT_REGION", "us-west-2")
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        + urllib.parse.quote(arn, safe="")
        + "/invocations?qualifier=DEFAULT"
    )


def token_for(persona: str) -> str:
    personas = run_eval.load_personas()
    if persona not in personas:
        sys.exit(f"unknown persona {persona!r}; known: {', '.join(personas)}")
    password = os.environ.get("EVAL_PASSWORD")
    secret = os.environ.get("COGNITO_CLIENT_SECRET")
    if not password or not secret:
        sys.exit("EVAL_PASSWORD / COGNITO_CLIENT_SECRET missing — source scripts/demo_env.sh")
    return login(personas[persona]["user"], password, secret)


async def run(persona: str, calls: list[str], resources: list[str], full: bool) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token_for(persona)}"}
    async with streamablehttp_client(mcp_url(), headers=headers, timeout=300) as (r, w, _):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            print(f"server: {init.serverInfo.name} · protocol {init.protocolVersion}")
            tools = await session.list_tools()
            print("tools:")
            for t in tools.tools:
                print(f"  {t.name:14} {(t.description or '').strip().splitlines()[0][:110]}")
            res = await session.list_resources()
            print("resources:", ", ".join(str(x.uri) for x in res.resources))
            for uri in resources or (["data-agent://context"] if not calls else []):
                out = await session.read_resource(uri)
                text = out.contents[0].text
                print(f"\n── resource {uri}\n{text if full else text[:1200]}")
            for call in calls:
                name, _, raw = call.partition(" ")
                args = json.loads(raw) if raw.strip() else {}
                print(f"\n── call {name} {raw.strip()[:160]}")
                out = await session.call_tool(name, args)
                body = "".join(getattr(c, "text", "") for c in out.content)
                print(body if full else body[:1600])


def print_config() -> None:
    print(
        json.dumps(
            {
                "mcpServers": {
                    "data-agent": {
                        "type": "http",
                        "url": mcp_url(),
                        "headers": {"Authorization": "Bearer <your Cognito ID token>"},
                    }
                }
            },
            indent=2,
        )
    )
    print(
        "\nA client that implements the MCP OAuth flow discovers the authorization server\n"
        "from the 401's `WWW-Authenticate: … resource_metadata=…` and needs a pre-registered\n"
        "client id (Claude Code `--client-id`, Kiro `oauth.clientId`); Dynamic Client\n"
        "Registration is not offered by this Cognito pool."
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("persona", nargs="?")
    ap.add_argument("--call", action="append", default=[], metavar='"tool {json}"')
    ap.add_argument("--resource", action="append", default=[])
    ap.add_argument("--full", action="store_true", help="do not truncate output")
    ap.add_argument("--print-config", action="store_true")
    args = ap.parse_args()
    if args.print_config:
        print_config()
        return
    if not args.persona:
        ap.error("give a persona (or --print-config)")
    asyncio.run(run(args.persona, args.call, args.resource, args.full))


if __name__ == "__main__":
    main()
