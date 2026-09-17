#!/usr/bin/env python3
"""A local stdio MCP server that proxies to the deployed agent — the shape a
coding agent can use every day.

Why a proxy at all: the runtime's MCP endpoint is authenticated with a Cognito
**ID token** that lives 60 minutes, and a client config file holding a static
`Authorization: Bearer …` header therefore breaks every hour. This process mints
the token itself (Cognito USER_PASSWORD_AUTH with the persona's password read
from SSM Parameter Store at start-up), keeps ONE upstream MCP session open — so
the session's working-set handles (`_r_<n>`) stay readable across calls — and
reconnects with a fresh token when the old one is refused.

Nothing is written to disk: no token, no password, no secret. The client config
holds only this command line.

  claude mcp add --scope user data-agent -- \\
      /path/to/.venv/bin/python /path/to/scripts/mcp_stdio_proxy.py --persona finops

Environment (all optional; resolved from the deployment when unset):
  DATA_AGENT_PERSONA        persona name, default `finops`   (--persona wins)
  AGENT_MCP_RUNTIME_ARN     the MCP runtime, else looked up by name
  AGENT_REGION              default us-west-2
  COGNITO_POOL_ID / COGNITO_CLIENT_ID
                            else read from the runtime's own authorizer config
  DATA_AGENT_PASSWORD_PARAM SSM SecureString name,
                            default /duckdb-agent/eval/persona-password
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

REGION = os.environ.get("AGENT_REGION", "us-west-2")
PASSWORD_PARAM = os.environ.get("DATA_AGENT_PASSWORD_PARAM", "/duckdb-agent/eval/persona-password")


def log(message: str) -> None:
    """Diagnostics go to stderr — stdout is the MCP transport."""
    print(f"[data-agent proxy] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------- deployment
def resolve_runtime() -> str:
    arn = os.environ.get("AGENT_MCP_RUNTIME_ARN")
    if arn:
        return arn
    import boto3

    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    for page in control.get_paginator("list_agent_runtimes").paginate():
        for runtime in page.get("agentRuntimes", []):
            if runtime["agentRuntimeName"] == os.environ.get(
                "AGENT_MCP_RUNTIME_NAME", "duckdb_analyst_mcp"
            ):
                return runtime["agentRuntimeArn"]
    raise SystemExit("no MCP runtime found; set AGENT_MCP_RUNTIME_ARN")


def mcp_url(arn: str) -> str:
    quoted = urllib.parse.quote(arn, safe="")
    return (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{quoted}"
        "/invocations?qualifier=DEFAULT"
    )


def resolve_identity(arn: str) -> tuple[str, str]:
    """(pool id, app client id) — from the env, else from the runtime's own
    authorizer configuration, so a deployment is self-describing."""
    pool, client = os.environ.get("COGNITO_POOL_ID"), os.environ.get("COGNITO_CLIENT_ID")
    if pool and client:
        return pool, client
    import boto3

    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    cfg = control.get_agent_runtime(agentRuntimeId=arn.split("/")[-1]).get(
        "authorizerConfiguration", {}
    )
    jwt_cfg = cfg.get("customJWTAuthorizer", {})
    audience = (jwt_cfg.get("allowedAudience") or jwt_cfg.get("allowedClients") or [None])[0]
    # the pool id is the one path segment shaped like `<region>_<suffix>` —
    # taking a fixed index picks `.well-known` out of the discovery URL
    segments = jwt_cfg.get("discoveryUrl", "").split("/")
    found = next(
        (seg for seg in segments if re.fullmatch(r"[a-z]{2}-[a-z]+-\d_[A-Za-z0-9]+", seg)), None
    )
    pool = pool or found
    client = client or audience
    if not pool or not client:
        raise SystemExit("set COGNITO_POOL_ID and COGNITO_CLIENT_ID")
    return pool, client


def read_password() -> str:
    if os.environ.get("EVAL_PASSWORD"):
        return os.environ["EVAL_PASSWORD"]
    import boto3

    ssm = boto3.client("ssm", region_name=REGION)
    return ssm.get_parameter(Name=PASSWORD_PARAM, WithDecryption=True)["Parameter"]["Value"]


def read_client_secret(pool: str, client: str) -> str:
    if os.environ.get("COGNITO_CLIENT_SECRET"):
        return os.environ["COGNITO_CLIENT_SECRET"]
    import boto3

    idp = boto3.client("cognito-idp", region_name=REGION)
    return idp.describe_user_pool_client(UserPoolId=pool, ClientId=client)["UserPoolClient"][
        "ClientSecret"
    ]


# ------------------------------------------------------------------ upstream
class Upstream:
    """One long-lived MCP session to the runtime, owned by ONE task.

    The upstream client is an anyio context manager with its own task group, so
    it must be entered and used inside a single task — a session entered in one
    request handler and used in the next deadlocks (measured). Hence this shape:
    an owner task holds the session and serves (operation, args) requests from a
    queue, and the MCP handlers only await a future. The owner reconnects with a
    fresh token when the old one is refused (an expired ID token is a 401/403),
    which is also the only moment the session's `_r_<n>` handles are lost.
    """

    def __init__(self, url: str, persona: str, user: str, password: str, secret: str) -> None:
        self.url, self.persona, self.user = url, persona, user
        self._password, self._secret = password, secret
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        # The first upstream call pays for a Cognito login plus, if the microVM
        # is cold, the runtime's own start-up — more than a client's tools-fetch
        # timeout. So the session is warmed at launch and the tool list cached.
        self.tools: Any = None
        self.resources: Any = None

    def _token(self) -> str:
        from jwt_invoke import login  # scripts/jwt_invoke.py

        return login(self.user, self._password, self._secret)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._owner())

    async def warm(self) -> None:
        """Connect and cache the tool/resource lists, so the client's first
        request is answered from memory."""
        self.start()
        with contextlib.suppress(Exception):
            self.tools = await self.call("list_tools")
        with contextlib.suppress(Exception):
            self.resources = await self.call("list_resources")
        if self.tools is not None:
            log(f"ready: {len(self.tools.tools)} tools cached")

    async def call(self, what: str, *args, **kwargs):
        self.start()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put((what, args, kwargs, future))
        return await future

    async def _owner(self) -> None:
        """Connect, then serve queued operations until cancelled."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        while True:
            pending: tuple | None = None
            try:
                headers = {"Authorization": f"Bearer {await asyncio.to_thread(self._token)}"}
                async with streamablehttp_client(self.url, headers=headers, timeout=600) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        info = await session.initialize()
                        log(f"connected as {self.persona} → {info.serverInfo.name}")
                        while True:
                            what, args, kwargs, future = pending or await self._queue.get()
                            pending = None
                            try:
                                result = await getattr(session, what)(*args, **kwargs)
                            except Exception as e:  # noqa: BLE001 — decide by the error text
                                text = str(e)
                                if _looks_expired(text):
                                    log(f"upstream refused ({text[:70]}); reconnecting")
                                    pending = (what, args, kwargs, future)
                                    break  # leave the session, reconnect, retry once
                                if not future.done():
                                    future.set_exception(e)
                                continue
                            if not future.done():
                                future.set_result(result)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — connect failed: fail the caller, then retry
                log(f"upstream connection failed: {str(e)[:160]}")
                if pending is not None and not pending[3].done():
                    pending[3].set_exception(e)
                    pending = None
                await asyncio.sleep(2)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None


def _looks_expired(text: str) -> bool:
    return any(marker in text for marker in ("401", "403", "Unauthorized", "token"))


# ----------------------------------------------------------------- local view
def local_surface() -> tuple[list, list]:
    """The tool and resource list, derived from THIS repo's server module with no
    network at all — so a client's first `tools/list` is answered instantly even
    while the upstream session is still connecting (a cold microVM plus a Cognito
    login is longer than a client's tools-fetch timeout).

    It is not a guess: the proxy ships in the same repository as the MCP server,
    so the names, schemas and descriptions come from the same code the runtime
    runs. Governance is enforced upstream, never here; the moment the real
    session is up its list replaces this one.
    """
    from data_agent.scheduling import mcp as surface

    server = surface.build_server()

    async def _collect():
        return await server.list_tools(), await server.list_resources()

    return asyncio.run(_collect())


# --------------------------------------------------------------------- serve
async def serve(upstream: Upstream, local_tools: list, local_resources: list) -> None:
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    server = Server("data-agent (proxy)")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        if upstream.tools is not None:
            return upstream.tools.tools
        return local_tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> types.CallToolResult:
        # Return the upstream CallToolResult untouched: content AND
        # structuredContent, so a tool whose schema promises structured output
        # still satisfies its client through the proxy.
        return await upstream.call("call_tool", name, arguments or {})

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        if upstream.resources is not None:
            return upstream.resources.resources
        return local_resources

    @server.read_resource()
    async def read_resource(uri) -> str:
        contents = (await upstream.call("read_resource", uri)).contents
        return "".join(getattr(c, "text", "") for c in contents)

    warming = asyncio.create_task(upstream.warm())  # in parallel with the handshake
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        warming.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await warming


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--persona", default=os.environ.get("DATA_AGENT_PERSONA", "finops"))
    args = ap.parse_args()

    import run_eval  # scripts/run_eval.py — the persona → Cognito user table

    personas = run_eval.load_personas()
    if args.persona not in personas:
        raise SystemExit(f"unknown persona {args.persona!r}; known: {', '.join(personas)}")
    arn = resolve_runtime()
    pool, client = resolve_identity(arn)
    os.environ.setdefault("COGNITO_POOL_ID", pool)
    os.environ.setdefault("COGNITO_CLIENT_ID", client)
    os.environ.setdefault("AGENT_REGION", REGION)
    upstream = Upstream(
        mcp_url(arn),
        args.persona,
        personas[args.persona]["user"],
        read_password(),
        read_client_secret(pool, client),
    )
    log(f"persona {args.persona} ({personas[args.persona]['role']}), runtime {arn.split('/')[-1]}")

    local_tools, local_resources = local_surface()
    log(f"local surface: {len(local_tools)} tools (served until the session is up)")

    async def run() -> None:
        try:
            await serve(upstream, local_tools, local_resources)
        finally:
            await upstream.aclose()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
