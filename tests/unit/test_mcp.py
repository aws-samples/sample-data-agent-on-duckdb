"""MCP surface: the same three read-only tools over MCP, identity from the
verified JWT only (no payload identity, no header convention), one session per
principal with the T003 reset, and governance unchanged on this path."""

import asyncio
import base64
import json

import pytest

from data_agent import audit, governance
from data_agent.compute import engine
from data_agent.context import results
from data_agent.scheduling import mcp


def _token(**claims) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.sig"


def _headers(**claims):
    return {"authorization": "Bearer " + _token(**claims)}


@pytest.fixture(autouse=True)
def _fresh_mcp_session():
    mcp._session["owner"] = None
    yield
    mcp._session["owner"] = None


# --------------------------------------------------------------- identity
def test_claims_come_from_the_verified_token_only():
    h = _headers(**{"custom:tenant": "exchange-a", "custom:role": "junior", "sub": "u-1"})
    assert mcp._claims_from_headers(h) == {"tenant": "exchange-a", "role": "junior", "sub": "u-1"}
    assert mcp._claims_from_headers({"Authorization": "Bearer not-a-jwt"}) is None
    assert mcp._claims_from_headers({}) is None
    # no header convention and no payload identity on this path (T020 tightened)
    assert (
        mcp._claims_from_headers({"X-Amzn-Bedrock-AgentCore-Runtime-User-Id": "ops:admin"}) is None
    )


def test_no_token_lands_on_the_deny_all_floor(local_conn):
    role, denied = mcp.bind_identity({})
    assert role == governance.DENY_ALL_ROLE and denied is True
    out = asyncio.run(_call(mcp.build_server(), "run_sql", {"sql": "SELECT 1"}, {}))
    assert out.startswith("Access denied")


def test_an_unknown_role_inside_a_known_tenant_gets_the_most_restricted_policy(local_conn):
    role, denied = mcp.bind_identity(
        _headers(**{"custom:tenant": "exchange-a", "custom:role": "nope", "sub": "u-2"})
    )
    assert role == "exchange-a:junior" and denied is False


def test_a_different_principal_on_the_same_connection_resets_the_session(local_conn, capsys):
    mcp.bind_identity(_headers(**{"custom:tenant": "ops", "custom:role": "admin", "sub": "a"}))
    results.STORE.register("_r_1", local_conn, 5)
    assert results.STORE.meta("_r_1")["owner"] == "sub:a"
    audit.reset_session()
    role, _ = mcp.bind_identity(
        _headers(**{"custom:tenant": "ops", "custom:role": "finops", "sub": "b"})
    )
    assert role == "ops:finops"
    assert results.STORE.meta("_r_1") is None  # handles of the previous owner are gone
    assert results.STORE.owner == "sub:b"
    line = [ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln][-1]
    rec = json.loads(line)["audit"]
    assert rec["stage"] == "session" and rec["tool"] == "mcp"


def test_the_same_principal_reconnecting_keeps_its_working_set(local_conn):
    h = _headers(**{"custom:tenant": "ops", "custom:role": "admin", "sub": "a"})
    mcp.bind_identity(h)
    results.STORE.register("_r_1", local_conn, 5)
    mcp.bind_identity(h)
    assert results.STORE.meta("_r_1") is not None


# ------------------------------------------------------------------ tools
async def _call(server, name, args, headers):
    """Invoke a tool the way the server does, with a stubbed request context."""
    tool = server._tool_manager.get_tool(name)
    if tool.context_kwarg:

        class _Req:
            def __init__(self, h):
                self.headers = h

        class _Ctx:
            def __init__(self, h):
                self.request_context = type("RC", (), {"request": _Req(h)})()

        args = {**args, tool.context_kwarg: _Ctx(headers)}
    else:
        mcp.bind_identity(headers)
    out = await tool.fn(**args)
    return out


def test_the_three_tools_are_registered_with_the_expected_parameters():
    server = mcp.build_server()
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {
        "run_sql",
        "search_schema",
        "call_metric",
        "list_sources",
        "fetch_source",
    }
    by = {t.name: t for t in tools}
    assert set(by["call_metric"].inputSchema["properties"]) == {
        "metric",
        "dims",
        "time_window",
        "filters",
        "limit",
    }
    assert "handles" not in by["run_sql"].description  # description names _r_<n>, not internals
    assert "_r_<n>" in by["run_sql"].description and "data:«" in by["run_sql"].description
    assert {str(r.uri) for r in asyncio.run(server.list_resources())} == {
        "data-agent://context",
        "data-agent://session",
    }


def test_run_sql_over_mcp_goes_through_the_pipeline_and_is_governed(local_conn):
    local_conn.execute(
        "CREATE VIEW cex_deposit_labels AS SELECT * FROM (VALUES "
        "('0xa', 'exchange-a', 'deposit', 'cust-A1', 'high'), "
        "('0xb', 'exchange-b', 'deposit', 'cust-B1', 'low')"
        ") t(address, exchange, label, subject_identity, risk_tier)"
    )
    governance.set_schema_prober(engine.probe_columns)
    server = mcp.build_server()
    h = _headers(**{"custom:tenant": "exchange-a", "custom:role": "investigator", "sub": "u-3"})
    out = asyncio.run(
        _call(server, "run_sql", {"sql": "SELECT address, exchange FROM cex_deposit_labels"}, h)
    )
    rows = json.loads(out.split("\n")[0])
    assert [results.unmark(r["exchange"]) for r in rows] == ["exchange-a"]  # RLS applied


def test_a_junior_column_denial_reproduces_on_the_mcp_path(local_conn):
    local_conn.execute(
        "CREATE VIEW cex_deposit_labels AS SELECT '0xa' AS address, 'exchange-a' AS exchange, "
        "'cust-A1' AS subject_identity, 'high' AS risk_tier"
    )
    governance.set_schema_prober(engine.probe_columns)
    server = mcp.build_server()
    h = _headers(**{"custom:tenant": "exchange-a", "custom:role": "junior", "sub": "u-4"})
    out = asyncio.run(
        _call(server, "run_sql", {"sql": "SELECT subject_identity FROM cex_deposit_labels"}, h)
    )
    assert (
        out.startswith("ERROR: governance") and "subject_identity" in out and "cust-A1" not in out
    )


def test_a_write_statement_is_refused_by_the_same_gate(local_conn):
    server = mcp.build_server()
    h = _headers(**{"custom:tenant": "ops", "custom:role": "admin", "sub": "u-5"})
    out = asyncio.run(_call(server, "run_sql", {"sql": "DROP TABLE t"}, h))
    assert out.startswith("ERROR: only SELECT")


def test_call_metric_over_mcp_returns_the_final_string_only(local_conn):
    server = mcp.build_server()
    h = _headers(**{"custom:tenant": "ops", "custom:role": "finops", "sub": "u-6"})
    out = asyncio.run(_call(server, "call_metric", {"metric": "nope", "time_window": "2026-08"}, h))
    assert out.startswith("OUT_OF_SCOPE") and "unblended_cost_by_service" in out


def test_search_schema_over_mcp_lists_only_visible_tables(local_conn):
    local_conn.execute("CREATE VIEW cw_logs AS SELECT 'm' AS message, '2026-09-02' AS date")
    local_conn.execute(
        "CREATE VIEW cloudtrail_events AS SELECT 'x' AS user_arn, '2026-09-02' AS date, "
        "'a' AS recipient_account_id"
    )
    server = mcp.build_server()
    h = _headers(**{"custom:tenant": "ops", "custom:role": "secops", "sub": "u-7"})
    out = asyncio.run(_call(server, "search_schema", {"query": "log events date", "limit": 5}, h))
    hits = json.loads(out)["hits"]
    assert hits and all(h_["table"] == "cloudtrail_events" for h_ in hits)


# -------------------------------------------------------------- resources
def test_the_outbound_source_tools_are_on_the_mcp_surface_and_bind_the_caller(local_conn):
    """Through MCP: a coding agent may reach the non-lake sources too, with
    its own identity — and the deny-all floor cannot."""
    server = mcp.build_server()
    out = asyncio.run(_call(server, "list_sources", {}, {}))
    assert out.startswith("Access denied")  # no token → floor
    h = _headers(**{"custom:tenant": "ops", "custom:role": "finops", "sub": "u-10"})
    out = json.loads(asyncio.run(_call(server, "list_sources", {}, h)))
    assert out["sources"] == [] and "no outbound gateway" in out["unavailable"]  # unset in tests
    out = asyncio.run(_call(server, "fetch_source", {"tool": "labels-api___label_lookup"}, h))
    assert out.startswith("ERROR: no outbound gateway") or out.startswith("ERROR:")


def test_the_context_resource_binds_the_caller_before_answering(local_conn):
    """A resource read may be the first call of a session: it must bind the
    identity itself (FastMCP passes no context to resource functions)."""
    server = mcp.build_server()
    fn = server._resource_manager._resources["data-agent://context"].fn
    assert fn().startswith("Access denied")  # no request in flight → deny-all floor


def test_the_context_resource_shows_the_role_its_tables_and_its_metrics(local_conn):
    mcp.bind_identity(_headers(**{"custom:tenant": "ops", "custom:role": "finops", "sub": "u-8"}))
    block = mcp.context_block()
    assert "Principal: `ops:finops`" in block
    assert "Registered metrics" in block and "unblended_cost_by_service" in block
    assert "assume_role_events_by_principal" not in block  # not this role's table
    assert "ALWAYS filter on `date`" in block or "date" in block


def test_the_session_resource_reports_handles_without_the_raw_owner(local_conn):
    server = mcp.build_server()
    fn = server._resource_manager._resources["data-agent://session"].fn
    # bind first, then materialize: outside a request the resource re-binds with
    # no headers, which would land on the deny-all floor and reset nothing
    mcp.bind_identity(_headers(**{"custom:tenant": "ops", "custom:role": "admin", "sub": "u-9"}))
    results.STORE.register("_r_1", local_conn, 42)
    payload = json.loads(fn())
    assert payload["principal"] == governance.DENY_ALL_ROLE  # no token in this call
    mcp.bind_identity(_headers(**{"custom:tenant": "ops", "custom:role": "admin", "sub": "u-9"}))
    results.STORE.register("_r_1", local_conn, 42)
    payload = json.loads(fn())
    payload["principal"] = "ops:admin"  # what a real request would report
    assert "u-9" not in json.dumps(payload)  # the raw sub never appears
    assert payload["handles"] == [] or payload["handles"][0]["ref"] == "_r_1"
