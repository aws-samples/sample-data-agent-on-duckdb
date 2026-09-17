#!/usr/bin/env python3
"""Call the outbound Gateway directly, as the agent does.

The point of this script is to show WHO the source sees. The agent never holds a
source credential: it forwards the CALLER's Cognito token to the Gateway, the
Gateway validates that token per request and then uses its OWN outbound
credential (here its IAM role) to reach the Lambda / Redshift target. Running
this as different personas — and without a token — makes that boundary visible.

  source scripts/demo_env.sh
  python scripts/gateway_invoke.py --list                       # tools, as investigator
  python scripts/gateway_invoke.py --no-token                   # expect 401 + the challenge
  python scripts/gateway_invoke.py finops \\
      --call 'redshift___cost_total {"billing_period": "2026-08"}'
  python scripts/gateway_invoke.py investigator \\
      --call 'labels-api___screen_addresses
              {"addresses": ["0x8576acc5c05d6ce88f4e49bf65bdf0c62f91353c"]}'   # on the OFAC list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from mcp_invoke import token_for  # noqa: E402


def gateway_url() -> str:
    url = os.environ.get("GATEWAY_MCP_URL", "")
    if not url:
        sys.exit("GATEWAY_MCP_URL is unset — run `source scripts/demo_env.sh` first")
    return url


def unauthenticated() -> None:
    """No token: the 401 must carry the RFC 9728 pointer to the authorization
    server, which is how an MCP client knows where to log in."""
    req = urllib.request.Request(
        gateway_url(),
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # the URL is the deployment's Gateway endpoint (GATEWAY_MCP_URL, resolved from the
        # control plane by demo_env.sh), never user input
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310  # nosec B310 - fixed https AWS endpoint  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            print("unexpected success:", resp.status, resp.read()[:200])
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}")
        challenge = e.headers.get("www-authenticate") or e.headers.get("WWW-Authenticate")
        if challenge:
            print(f"WWW-Authenticate: {challenge}")
        print(e.read().decode()[:200])
        meta = gateway_url().rsplit("/", 1)[0] + "/.well-known/oauth-protected-resource"
        # RFC 9728 metadata document at the same Gateway host — derived from the endpoint above
        with urllib.request.urlopen(meta, timeout=30) as m:  # noqa: S310  # nosec B310 - fixed https AWS endpoint  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            print(f"\n{meta}\n{m.read().decode()}")


async def run(persona: str, calls: list[str], list_only: bool) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token_for(persona)}"}
    async with streamablehttp_client(gateway_url(), headers=headers, timeout=180) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"gateway tools visible to {persona}:")
            for t in tools.tools:
                if t.name.startswith("x_amz_"):
                    continue  # the gateway's own semantic-search tool
                print(f"  {t.name:34} {(t.description or '').strip().splitlines()[0][:90]}")
            if list_only:
                return
            for call in calls:
                name, _, raw = call.partition(" ")
                print(f"\n── {name} {raw.strip()[:140]}")
                out = await session.call_tool(name, json.loads(raw) if raw.strip() else {})
                print("".join(getattr(c, "text", "") for c in out.content)[:1500])


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("persona", nargs="?", default="investigator")
    ap.add_argument("--call", action="append", default=[], metavar='"tool {json}"')
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-token", action="store_true", help="show the 401 challenge instead")
    args = ap.parse_args()
    if args.no_token:
        unauthenticated()
        return
    asyncio.run(run(args.persona, args.call, args.list or not args.call))


if __name__ == "__main__":
    main()
