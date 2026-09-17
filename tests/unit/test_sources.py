"""Outbound sources: rows that are not on S3, fetched through the Gateway
with the caller's identity and landed as a session handle. No ATTACH, no source
credentials in the process, tool results untrusted."""

import json
import threading

import pytest

import agent as agent_mod
from data_agent import audit, governance
from data_agent.compute import engine
from data_agent.context import prompt, results
from data_agent.scheduling import gateway_client

ROWS = [
    {"address": "0xa", "sanctioned": True, "program": "OFAC-SDN", "risk_tier": "high"},
    {"address": "0xb", "sanctioned": False, "program": None, "label": {"nested": 1}},
]


@pytest.fixture(autouse=True)
def _no_real_gateway(monkeypatch):
    monkeypatch.setattr(gateway_client, "GATEWAY_MCP_URL", "https://gw.example/mcp")
    gateway_client.set_bearer("token-of-the-caller")
    yield
    gateway_client.set_bearer(None)


# ------------------------------------------------------------- landing rows
def test_tool_rows_become_a_handle_the_model_can_join(local_conn):
    results.STORE.bind("sub:u-1")
    res = results.materialize_rows(
        local_conn, ROWS, "labels-api___screen_addresses", threading.Lock()
    )
    text = results.format_result(res, governed=False)
    assert res["ref"] == "_r_1" and results.STORE.meta("_r_1")["owner"] == "sub:u-1"
    assert json.loads(text.split("\n")[1])["source"] == "labels-api___screen_addresses"
    assert "untrusted evidence" in text and f"JOIN {res['ref']} ON" in text
    assert "do NOT retype" in text or "Do NOT retype" in text
    # the rows are queryable SQL, and every text cell is marked
    rows = local_conn.execute("SELECT address, program FROM _r_1 ORDER BY address").fetchall()
    assert rows == [("0xa", "OFAC-SDN"), ("0xb", None)]
    assert '"address": "data:«0xa»"' in text
    assert '"label": "data:«{\\"nested\\": 1}»"' in text  # nested values become JSON text


def test_hostile_keys_and_values_cannot_become_sql(local_conn):
    results.STORE.bind("sub:u-2")
    hostile = [
        {"addr; DROP TABLE t --": "x", "ok": "'); DROP TABLE t; --"},
        {"addr; DROP TABLE t --": "y"},
    ]
    res = results.materialize_rows(local_conn, hostile, "src", threading.Lock())
    cols = [c[0] for c in local_conn.execute(f"DESCRIBE {res['ref']}").fetchall()]
    assert cols == ["addr__DROP_TABLE_t___", "ok"]  # sanitized identifiers
    assert local_conn.execute("SELECT count(*) FROM t").fetchone()[0] == 500  # t survives
    assert local_conn.execute(f"SELECT ok FROM {res['ref']} WHERE ok IS NOT NULL").fetchone()[
        0
    ] == ("'); DROP TABLE t; --")  # stored as a value


def test_an_empty_result_is_not_a_handle(local_conn):
    res = results.materialize_rows(local_conn, [], "src", threading.Lock())
    assert res["ref"] is None and res["total_rows"] == 0


def test_a_large_tool_result_arrives_as_a_summary(local_conn, monkeypatch):
    monkeypatch.setattr(results, "MAX_RESULT_ROWS", 5)
    results.STORE.bind("sub:u-3")
    rows = [{"i": i, "v": f"row-{i}"} for i in range(50)]
    res = results.materialize_rows(local_conn, rows, "src", threading.Lock())
    assert res["shape"] == "summary" and res["total_rows"] == 50 and res["ref"]
    text = results.format_result(res, governed=False)
    assert "session temp table" in text and "untrusted evidence" in text


# ------------------------------------------------------------ the tool path
def _fetch(tool="labels-api___screen_addresses", args=None, rows=None, raises=None):
    def fake(t, a=None):
        if raises:
            raise raises
        assert t == tool
        return ROWS if rows is None else rows

    gateway_client_call = gateway_client.call_tool
    gateway_client.call_tool = fake
    try:
        return agent_mod.fetch_source._tool_func(tool=tool, args=args or {})
    finally:
        gateway_client.call_tool = gateway_client_call


def test_fetch_source_lands_rows_and_audits_the_user_the_tool_and_the_degradation(
    local_conn, capsys
):
    governance.set_active_role("exchange-a:investigator")
    results.STORE.bind("sub:u-4")
    audit.reset_session()
    out = _fetch()
    assert "data:«0xa»" in out and results.STORE.meta("_r_1")
    rec = json.loads([ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln][-1])[
        "audit"
    ]
    assert rec["tool"] == "fetch_source" and rec["source"] == "labels-api___screen_addresses"
    assert rec["degraded"] == "2lo"  # this source authenticates as a service, not as the user
    assert rec["user_sub"] == results.owner_id("sub:u-4") and rec["materialized"] == "_r_1"
    assert "0xa" not in json.dumps(rec)  # the SQL field holds the CALL, not the rows


def test_a_source_failure_is_reported_not_raised(local_conn, capsys):
    governance.set_active_role("ops:admin")
    out = _fetch(raises=gateway_client.SourceError("source tool refused the call: quota"))
    assert out.startswith("ERROR: source tool refused") and "quota" in out
    rec = json.loads([ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln][-1])[
        "audit"
    ]
    assert rec["stage"] == "source" and rec["tool"] == "fetch_source"


def test_the_deny_all_floor_cannot_reach_a_source(local_conn):
    governance.set_active_role(governance.DENY_ALL_ROLE)
    assert agent_mod.fetch_source._tool_func(tool="labels-api___label_lookup").startswith(
        "ERROR: no access"
    )


# ------------------------------------------------------- client-side checks
def test_no_gateway_configured_is_a_clear_refusal(monkeypatch):
    monkeypatch.setattr(gateway_client, "GATEWAY_MCP_URL", "")
    with pytest.raises(gateway_client.SourceError, match="no outbound gateway"):
        gateway_client.call_tool("labels-api___label_lookup", {})


def test_no_caller_token_means_no_outbound_call():
    gateway_client.set_bearer(None)
    with pytest.raises(gateway_client.SourceError, match="no caller token"):
        gateway_client.call_tool("labels-api___label_lookup", {})
    assert not gateway_client.have_bearer()


def test_a_bad_tool_name_never_reaches_the_wire():
    with pytest.raises(gateway_client.SourceError, match="not a valid gateway tool name"):
        gateway_client.call_tool("labels-api; rm -rf /", {})


@pytest.mark.parametrize(
    "payload, msg",
    [
        ('{"error": "rate limited"}', "refused the call"),
        ("not json at all", "did not return JSON"),
        ('{"rows": [1, 2]}', "shape this layer cannot land"),
        ('[{"a": 1}, 7]', "shape this layer cannot land"),
    ],
)
def test_unusable_source_payloads_are_refused_verbatim(payload, msg, monkeypatch):
    monkeypatch.setattr(gateway_client, "_call_async", None)

    async def fake(tool, args):
        return payload

    monkeypatch.setattr(gateway_client, "_call_async", fake)
    with pytest.raises(gateway_client.SourceError, match=msg):
        gateway_client.call_tool("labels-api___label_lookup", {})


def test_a_dict_without_a_rows_list_is_treated_as_one_row(monkeypatch):
    """A source that answers with a single record (or with `rows: null` beside
    the record's fields) still lands as a one-row table, not an error."""

    async def one(tool, args):
        return json.dumps({"rows": "a string", "address": "0xa", "sanctioned": True})

    monkeypatch.setattr(gateway_client, "_call_async", one)
    assert gateway_client.call_tool("t", {}) == [
        {"rows": "a string", "address": "0xa", "sanctioned": True}
    ]


def test_row_shapes_the_layer_accepts(monkeypatch):
    async def fake(tool, args):
        return json.dumps({"rows": [{"a": 1}], "count": 1})

    monkeypatch.setattr(gateway_client, "_call_async", fake)
    assert gateway_client.call_tool("t", {}) == [{"a": 1}]

    async def bare_list(tool, args):
        return json.dumps([{"a": 1}, {"a": 2}])

    monkeypatch.setattr(gateway_client, "_call_async", bare_list)
    assert len(gateway_client.call_tool("t", {})) == 2

    async def single(tool, args):
        return json.dumps({"a": 1, "rows": None})

    monkeypatch.setattr(gateway_client, "_call_async", single)
    assert gateway_client.call_tool("t", {}) == [{"a": 1, "rows": None}]


def test_too_many_rows_is_refused(monkeypatch):
    monkeypatch.setattr(gateway_client, "MAX_TOOL_ROWS", 3)

    async def many(tool, args):
        return json.dumps({"rows": [{"i": i} for i in range(10)]})

    monkeypatch.setattr(gateway_client, "_call_async", many)
    with pytest.raises(gateway_client.SourceError, match="GATEWAY_MAX_TOOL_ROWS"):
        gateway_client.call_tool("t", {})


def test_degradation_marking_follows_the_configured_sources(monkeypatch):
    assert gateway_client.degraded("labels-api___screen_addresses") == "2lo"
    monkeypatch.setattr(gateway_client, "DEGRADED_TOOLS", ("other",))
    assert gateway_client.degraded("labels-api___screen_addresses") is None


def test_list_sources_reports_unavailability_instead_of_raising(monkeypatch):
    monkeypatch.setattr(gateway_client, "GATEWAY_MCP_URL", "")
    out = json.loads(agent_mod.list_sources._tool_func())
    assert out["sources"] == [] and "no outbound gateway" in out["unavailable"]


# ------------------------------------------------------------------ wiring
def test_the_tools_are_registered_and_the_prompt_teaches_lake_first():
    assert {"list_sources", "fetch_source"} <= set(agent_mod.agent.tool_names)
    assert "The lake first" in prompt.TEMPLATE and "fetch_source" in prompt.TEMPLATE
    assert (
        "no source credentials" in gateway_client.__doc__
        or "holds **no source" in gateway_client.__doc__
    )


def test_the_bearer_is_dropped_when_the_session_owner_changes(local_conn, monkeypatch):
    from data_agent.scheduling import runtime

    monkeypatch.setattr(runtime, "_session", {"owner": None})
    gateway_client.set_bearer("first-users-token")
    assert runtime._bind_session("sub:a") is False
    assert runtime._bind_session("sub:b") is True  # owner changed → caller resets
    called = {}
    monkeypatch.setattr(engine, "reset", lambda: called.setdefault("engine", True))
    runtime._reset_session_state(
        type("A", (), {"messages": type("M", (), {"clear": lambda self: None})()})(),
        type("C", (), {"stubbed": 0, "reclaimed_bytes": 0})(),
    )
    assert not gateway_client.have_bearer() and called["engine"]
