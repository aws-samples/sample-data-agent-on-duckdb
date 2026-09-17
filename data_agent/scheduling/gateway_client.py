"""Outbound MCP client: sources that are not on S3.

A lake is not the whole estate. A sanctions/labels feed is an HTTP API, a
warehouse is a warehouse — and both must be reachable WITHOUT the agent
becoming an ETL tool or holding their credentials. The shape:

    model → `fetch_source` tool → AgentCore Gateway (MCP, the CALLER's token)
          → the source's own credential lives in the Gateway
          → JSON rows → `context/results.materialize_rows` → `_r_<n>`
          → ordinary SQL joins them with the lake tables, in-process

What this is NOT: a DuckDB adapter (nothing is ATTACHed; see docs/design.md),
and not a silent fallback inside `run_sql` (it is a separate, model-visible
tool; routing stays outside the engine).

Identity: the Gateway validates the user's JWT per request, so the token the
agent forwards is the one the caller presented — the same token this process
already decoded for claims, no new secret. The agent process holds **no source
credentials**: the Gateway (or the Identity token vault) does. A source whose
outbound credential is a service identity (2LO, API key, or the gateway's IAM
role, as the labels API is today) cannot be per-user at the source, so the
result is marked `degraded=2lo` in the audit line and the row filter that
applies to the caller is applied on `_r_<n>` afterwards.

Trust: tool results are UNTRUSTED CONTENT, exactly like table rows — they are
marked `data:«…»` when the model reads them and never treated as
instructions.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

GATEWAY_MCP_URL = os.environ.get("GATEWAY_MCP_URL", "")
GATEWAY_TIMEOUT_S = float(os.environ.get("GATEWAY_TIMEOUT_S", "60"))
# tools whose outbound credential is a service identity, not the caller's
DEGRADED_TOOLS = tuple(
    t.strip()
    for t in os.environ.get("GATEWAY_DEGRADED_TOOLS", "labels-api").split(",")
    if t.strip()
)
MAX_TOOL_ROWS = int(os.environ.get("GATEWAY_MAX_TOOL_ROWS", "10000"))
_TOOL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")

# The caller's bearer token for this session, set by the scheduling layer when a
# request arrives. Module-level because one microVM serves one session (the same
# scope as the active role); cleared on an owner change.
_bearer: dict[str, str | None] = {"token": None}


def set_bearer(token: str | None) -> None:
    _bearer["token"] = token or None


def have_bearer() -> bool:
    return bool(_bearer["token"])


class SourceError(RuntimeError):
    """The outbound call could not be made or the source refused it."""


def _check_configured(tool: str) -> None:
    if not GATEWAY_MCP_URL:
        raise SourceError(
            "no outbound gateway is configured (GATEWAY_MCP_URL is unset) — this deployment "
            "reads the lake only"
        )
    if not _TOOL_RE.match(tool):
        raise SourceError(f"tool name {tool!r} is not a valid gateway tool name")
    if not have_bearer():
        raise SourceError(
            "no caller token is bound to this session, so an outbound call cannot carry your "
            "identity — outbound sources are unavailable on this path"
        )


def degraded(tool: str) -> str | None:
    """`2lo` when this tool's outbound credential is a service identity."""
    return "2lo" if any(tool.startswith(p) for p in DEGRADED_TOOLS) else None


async def _call_async(tool: str, args: dict) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {_bearer['token']}"}
    async with streamablehttp_client(
        GATEWAY_MCP_URL, headers=headers, timeout=GATEWAY_TIMEOUT_S
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            text = "".join(getattr(c, "text", "") for c in result.content)
            if getattr(result, "isError", False):
                raise SourceError(f"source tool {tool!r} returned an error: {text[:300]}")
            return text


def list_tools() -> list[dict]:
    """The read-only tools this deployment's gateway exposes (names + docs)."""
    _check_configured("x")

    async def _go():
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {_bearer['token']}"}
        async with streamablehttp_client(
            GATEWAY_MCP_URL, headers=headers, timeout=GATEWAY_TIMEOUT_S
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [
                    {"tool": t.name, "description": (t.description or "")[:300]}
                    for t in tools.tools
                    if not t.name.startswith("x_amz_")  # the gateway's own search tool
                ]

    return asyncio.run(_go())


def call_tool(tool: str, args: dict | None = None) -> list[dict]:
    """Call one gateway tool with the caller's token; return its rows.

    A source returns JSON. Accepted shapes: `{"rows": [...]}`, a bare list of
    objects, or a single object (one row). Anything else is an error the model
    reads verbatim — we do not guess a schema.
    """
    _check_configured(tool)
    text = asyncio.run(_call_async(tool, args or {}))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise SourceError(f"source tool {tool!r} did not return JSON: {text[:200]}") from e
    if isinstance(payload, dict) and payload.get("error"):
        raise SourceError(f"source tool {tool!r} refused the call: {payload['error']}")
    if isinstance(payload, dict):
        # `{"rows": [...]}` is the documented shape; a dict without usable rows
        # IS one row (a source that returns a single record, `rows: null` and all)
        rows = payload["rows"] if isinstance(payload.get("rows"), list | dict) else payload
    else:
        rows = payload
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise SourceError(
            f"source tool {tool!r} returned a shape this layer cannot land as a table "
            '(expected {"rows": [ {..}, .. ]} or a list of objects)'
        )
    if len(rows) > MAX_TOOL_ROWS:
        raise SourceError(
            f"source tool {tool!r} returned {len(rows)} rows, more than GATEWAY_MAX_TOOL_ROWS "
            f"({MAX_TOOL_ROWS}) — narrow the request"
        )
    return rows
